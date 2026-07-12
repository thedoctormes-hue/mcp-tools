package main

// PortMap is the minimal interface the loader needs to maintain the allowlist.
// It is satisfied by an in-memory map (tests) and by a real eBPF map
// (loader.go). Keeping it small lets us unit-test the enforcement logic
// without ever touching a live kernel.
type PortMap interface {
	// Put marks `port` as allowed.
	Put(port uint16) error
	// Delete removes `port` from the allowlist.
	Delete(port uint16) error
	// Has reports whether `port` is currently allowed.
	Has(port uint16) bool
	// Keys returns all currently-allowed ports (for reconcile diffing).
	Keys() []uint16
	// Close releases the underlying map (no-op for the mock).
	Close() error
}

// memPortMap is an in-memory PortMap used by unit tests and as a safe
// stand-in when no eBPF program is loaded.
type memPortMap struct {
	m map[uint16]struct{}
}

// NewMemPortMap returns an empty in-memory allowlist.
func NewMemPortMap() *memPortMap {
	return &memPortMap{m: make(map[uint16]struct{})}
}

func (m *memPortMap) Put(port uint16) error {
	m.m[port] = struct{}{}
	return nil
}

func (m *memPortMap) Delete(port uint16) error {
	delete(m.m, port)
	return nil
}

func (m *memPortMap) Has(port uint16) bool {
	_, ok := m.m[port]
	return ok
}

func (m *memPortMap) Keys() []uint16 {
	keys := make([]uint16, 0, len(m.m))
	for k := range m.m {
		keys = append(keys, k)
	}
	return keys
}

func (m *memPortMap) Close() error { return nil }

// reconcile makes `pm` exactly match `desired`: inserts missing ports and
// removes stale ones. This is what the loader calls on every reload so the
// kernel allowlist tracks leases.json + policy in real time.
func reconcile(pm PortMap, desired map[uint16]struct{}) error {
	for port := range desired {
		if err := pm.Put(port); err != nil {
			return err
		}
	}
	for _, port := range pm.Keys() {
		if _, ok := desired[port]; !ok {
			if err := pm.Delete(port); err != nil {
				return err
			}
		}
	}
	return nil
}
