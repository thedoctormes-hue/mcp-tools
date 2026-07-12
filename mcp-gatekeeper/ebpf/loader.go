package main

import (
	"fmt"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
)

// ebpfPortMap adapts a real eBPF hash map to the PortMap interface.
type ebpfPortMap struct {
	m *ebpf.Map
}

func (e *ebpfPortMap) Put(port uint16) error {
	var v uint8 = 1
	return e.m.Put(port, v)
}

func (e *ebpfPortMap) Delete(port uint16) error {
	return e.m.Delete(port)
}

func (e *ebpfPortMap) Has(port uint16) bool {
	var v uint8
	return e.m.Lookup(port, &v) == nil
}

func (e *ebpfPortMap) Keys() []uint16 {
	var out []uint16
	it := e.m.Iterate()
	var k uint16
	for it.Next(&k, nil) {
		out = append(out, k)
	}
	return out
}

func (e *ebpfPortMap) Close() error { return nil } // owned by the Collection

// Loader owns the loaded eBPF collection, the attached LSM hook, and the
// allowlist map. It is built BEFORE attach so the allowlist can be populated
// with zero "empty window" where every bind would be denied.
type Loader struct {
	coll     *ebpf.Collection
	lnk      *link.Link
	ports    *ebpfPortMap
	settings *ebpf.Map
}

// NewLoader loads bind.o, validates the maps exist, and sets the enforce/
// audit flag. It does NOT attach the hook yet — call Attach() after the
// allowlist has been populated via Reconcile().
func NewLoader(objPath string, enforce bool) (*Loader, error) {
	spec, err := ebpf.LoadCollectionSpec(objPath)
	if err != nil {
		return nil, fmt.Errorf("load eBPF object %q: %w", objPath, err)
	}

	coll, err := ebpf.NewCollection(spec)
	if err != nil {
		return nil, fmt.Errorf("create eBPF collection: %w", err)
	}

	am, ok := coll.Maps["allowed_ports"]
	if !ok {
		coll.Close()
		return nil, fmt.Errorf("eBPF object missing map 'allowed_ports'")
	}
	sm, ok := coll.Maps["settings"]
	if !ok {
		coll.Close()
		return nil, fmt.Errorf("eBPF object missing map 'settings'")
	}

	l := &Loader{
		coll:     coll,
		ports:    &ebpfPortMap{m: am},
		settings: sm,
	}

	if err := l.setEnforce(enforce); err != nil {
		coll.Close()
		return nil, fmt.Errorf("set enforce flag: %w", err)
	}
	return l, nil
}

// setEnforce writes settings[0] (1 = enforce, 0 = audit).
func (l *Loader) setEnforce(enforce bool) error {
	var v uint64
	if enforce {
		v = 1
	}
	return l.settings.Put(uint32(0), v)
}

// Reconcile makes the kernel allowlist exactly match `desired`.
func (l *Loader) Reconcile(desired map[uint16]struct{}) error {
	return reconcile(l.ports, desired)
}

// Has reports whether `port` is currently allowed (for tests/diagnostics).
func (l *Loader) Has(port uint16) bool { return l.ports.Has(port) }

// DeniedCount returns the running count of bind attempts on non-allowed
// ports (the audit counter). Useful to confirm enforcement is doing something
// and to spot false positives before switching audit -> enforce.
func (l *Loader) DeniedCount() (uint64, error) {
	var v uint64
	if err := l.settings.Lookup(uint32(1), &v); err != nil {
		return 0, err
	}
	return v, nil
}

// Attach installs the LSM hook. Call ONLY after Reconcile() has populated
// the allowlist, so there is no window where the map is empty.
func (l *Loader) Attach() error {
	prog, ok := l.coll.Programs["restrict_socket_bind"]
	if !ok {
		return fmt.Errorf("eBPF object missing program 'restrict_socket_bind'")
	}
	lnk, err := link.AttachLSM(link.LSMOptions{Program: prog})
	if err != nil {
		return fmt.Errorf("attach LSM socket_bind: %w", err)
	}
	l.lnk = &lnk
	return nil
}

// Close detaches the hook and frees the collection.
func (l *Loader) Close() {
	if l.lnk != nil {
		(*l.lnk).Close()
		l.lnk = nil
	}
	if l.coll != nil {
		l.coll.Close()
		l.coll = nil
	}
}
