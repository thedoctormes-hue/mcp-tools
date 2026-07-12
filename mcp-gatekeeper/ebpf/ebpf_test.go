package main

import (
	"testing"
)

// --- helpers to build fixtures ---------------------------------------------

func testPolicy() *Policy {
	p := &Policy{}
	p.Gatekeeper.ListenPort = 8888
	p.Agents = []Agent{{ID: "raven", PortRange: [2]int{8080, 8099}}}
	p.Reserve.BlockPrivilegedBelow = 1024
	p.Reserve.BlockedPorts = []int{80, 443, 5432, 8086, 8087, 8888, 9100}
	return p
}

func ptrPort(p int) *int { return &p }

func testLeases() *Leases {
	return &Leases{Leases: []Lease{
		{RequestID: "rk1", Agent: "raven", Kind: "service", Port: ptrPort(8081)},
		{RequestID: "rk2", Agent: "raven", Kind: "port", Port: ptrPort(8082)},
		{RequestID: "rk3", Agent: "owl", Kind: "timer", Port: nil},   // no port -> ignored
		{RequestID: "rk4", Agent: "owl", Kind: "service", Port: nil}, // nil port -> ignored
		{RequestID: "rk5", Agent: "kot", Kind: "service", Port: ptrPort(8140)},
	}}
}

// --- ComputeAllowedPorts ------------------------------------------------

func TestComputeAllowedPorts_BlockedIncluded(t *testing.T) {
	p := testPolicy()
	ls := testLeases()
	got := ComputeAllowedPorts(p, ls, nil)
	for _, pt := range p.Reserve.BlockedPorts {
		if _, ok := got[uint16(pt)]; !ok {
			t.Errorf("blocked port %d missing from allowlist", pt)
		}
	}
}

func TestComputeAllowedPorts_LeasePortsIncluded(t *testing.T) {
	p := testPolicy()
	ls := testLeases()
	got := ComputeAllowedPorts(p, ls, nil)
	for _, want := range []int{8081, 8082, 8140} {
		if _, ok := got[uint16(want)]; !ok {
			t.Errorf("active lease port %d missing from allowlist", want)
		}
	}
	// timer-only and nil-port leases must NOT appear
	if _, ok := got[uint16(0)]; ok {
		t.Errorf("nil-port lease should not add port 0")
	}
}

func TestComputeAllowedPorts_RandomDenied(t *testing.T) {
	p := testPolicy()
	ls := testLeases()
	got := ComputeAllowedPorts(p, ls, nil)
	// 5000 is neither reserved nor leased -> must be denied (absent)
	if _, ok := got[uint16(5000)]; ok {
		t.Errorf("port 5000 must NOT be in allowlist")
	}
	// An agent port in-range but WITHOUT a lease must also be absent.
	if _, ok := got[uint16(8085)]; ok {
		t.Errorf("un-leased port 8085 must NOT be in allowlist (enforcement!)")
	}
}

func TestComputeAllowedPorts_Extra(t *testing.T) {
	p := testPolicy()
	ls := testLeases()
	got := ComputeAllowedPorts(p, ls, []int{22, 53})
	if _, ok := got[uint16(22)]; !ok {
		t.Errorf("extra port 22 missing")
	}
	if _, ok := got[uint16(53)]; !ok {
		t.Errorf("extra port 53 missing")
	}
}

func TestComputeAllowedPorts_InvalidFiltered(t *testing.T) {
	p := testPolicy()
	p.Reserve.BlockedPorts = []int{80, -5, 70000} // bad values
	got := ComputeAllowedPorts(p, &Leases{}, nil)
	// -5 and 70000 are outside uint16 range and must be dropped.
	if _, ok := got[uint16(80)]; !ok {
		t.Errorf("valid port 80 should remain")
	}
	// With the gatekeeper listen port (8888) + 80 only.
	if len(got) != 2 {
		t.Errorf("expected exactly {80,8888} after filtering, got %v", got)
	}
}

// --- reconcile + mock map (the eBPF-map logic, without a kernel) ----------

func TestReconcile_MockMapAllowedDenied(t *testing.T) {
	mock := NewMemPortMap()
	p := testPolicy()
	ls := testLeases()
	desired := ComputeAllowedPorts(p, ls, []int{22})

	if err := reconcile(mock, desired); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	// Allowed set present
	if !mock.Has(8081) {
		t.Errorf("lease port 8081 should be allowed in mock map")
	}
	if !mock.Has(22) {
		t.Errorf("extra port 22 should be allowed in mock map")
	}
	if !mock.Has(8888) {
		t.Errorf("gatekeeper listen port 8888 should be allowed")
	}
	// Non-allowed absent
	if mock.Has(5000) {
		t.Errorf("port 5000 must be denied (absent from mock map)")
	}
	if mock.Has(8085) {
		t.Errorf("un-leased 8085 must be denied (absent from mock map)")
	}
}

func TestReconcile_RemovesStale(t *testing.T) {
	mock := NewMemPortMap()
	// Pre-populate a stale port that is no longer desired.
	if err := mock.Put(9999); err != nil {
		t.Fatal(err)
	}
	desired := map[uint16]struct{}{8081: {}}
	if err := reconcile(mock, desired); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if mock.Has(9999) {
		t.Errorf("stale port 9999 should have been removed")
	}
	if !mock.Has(8081) {
		t.Errorf("desired port 8081 should remain")
	}
}

// --- parsePorts -----------------------------------------------------------

func TestParsePorts(t *testing.T) {
	got, err := parsePorts("22, 53 ,8080")
	if err != nil {
		t.Fatal(err)
	}
	want := []int{22, 53, 8080}
	if len(got) != len(want) {
		t.Fatalf("got %v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("index %d: got %d want %d", i, got[i], want[i])
		}
	}
	if _, err := parsePorts("22,abc"); err == nil {
		t.Errorf("expected error on non-numeric port")
	}
	if _, err := parsePorts(""); err != nil {
		t.Errorf("empty string should not error: %v", err)
	}
}

// --- currentListeningPorts (read-only /proc parse, no kernel mutation) -----

func TestCurrentListeningPorts_Smoke(t *testing.T) {
	ports, err := currentListeningPorts()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// We cannot assert a specific port, but the parse must not panic and
	// must return a plausible count. It's fine if 0 (no listeners).
	for _, p := range ports {
		if p < 0 || p > 65535 {
			t.Errorf("parsed out-of-range port %d", p)
		}
	}
}
