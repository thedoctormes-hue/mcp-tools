// Package gatekeeper-ebpf is the kernel-level enforcement loader for
// mcp-gatekeeper (ADR-0055, Фаза 3). This file holds the PURE, testable
// logic: loading the policy + leases and computing the allowlist. No eBPF /
// kernel imports here so unit tests run in plain user-space.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// ---------------------------------------------------------------------------
// Policy (subset we care about for enforcement)
// ---------------------------------------------------------------------------

// Agent mirrors policy_v1.yaml agents[].
type Agent struct {
	ID        string `yaml:"id"`
	Name      string `yaml:"name"`
	PortRange [2]int `yaml:"port_range"`
}

// Reserve mirrors policy.reserve (blocked_ports are system-reserved ports that
// legitimately must be bindable, e.g. PostgreSQL/5432, node_exporter/9100).
type Reserve struct {
	BlockPrivilegedBelow int   `yaml:"block_privileged_below"`
	BlockedPorts         []int `yaml:"blocked_ports"`
}

// Policy is the parsed policy_v1.yaml (only the fields relevant to eBPF).
type Policy struct {
	Gatekeeper struct {
		ListenPort int `yaml:"listen_port"`
	} `yaml:"gatekeeper"`
	Agents  []Agent `yaml:"agents"`
	Reserve Reserve `yaml:"reserve"`
}

// LoadPolicy reads and parses a policy_v1.yaml file.
func LoadPolicy(path string) (*Policy, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read policy %q: %w", path, err)
	}
	var p Policy
	if err := yaml.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("parse policy %q: %w", path, err)
	}
	return &p, nil
}

// ---------------------------------------------------------------------------
// Leases (data/leases.json)
// ---------------------------------------------------------------------------

// Lease mirrors one entry written by mcp-gatekeeper-server.py.
type Lease struct {
	RequestID  string  `json:"request_id"`
	Agent      string  `json:"agent"`
	ProjectID  string  `json:"project_id"`
	Kind       string  `json:"kind"` // "port" | "timer" | "service"
	Port       *int    `json:"port"` // nil for timer-only leases
	WhatFor    string  `json:"what_for"`
	RunAs      string  `json:"run_as"`
	IssuedUser string  `json:"issued_user"`
	Bypass     *string `json:"bypass"`
}

// Leases is the parsed data/leases.json envelope.
type Leases struct {
	Version int     `json:"version"`
	SavedAt string  `json:"saved_at"`
	Leases  []Lease `json:"leases"`
}

// LoadLeases reads and parses a leases.json file. An empty/missing file is
// treated as "no active leases" (not a fatal error) so the loader can still
// enforce the reserved-port baseline.
func LoadLeases(path string) (*Leases, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return &Leases{Leases: nil}, nil
		}
		return nil, fmt.Errorf("read leases %q: %w", path, err)
	}
	if len(raw) == 0 {
		return &Leases{Leases: nil}, nil
	}
	var ls Leases
	if err := json.Unmarshal(raw, &ls); err != nil {
		return nil, fmt.Errorf("parse leases %q: %w", path, err)
	}
	return &ls, nil
}

// ---------------------------------------------------------------------------
// Allowlist computation
// ---------------------------------------------------------------------------

// ComputeAllowedPorts returns the set of ports that are PERMITTED to bind.
//
// Model (Kernel-level enforcement, ADR-0055 Фаза 3): only ports that are
// either system-reserved (reserve.blocked_ports — bound by legitimate
// services like PostgreSQL/node_exporter/the gatekeeper itself) or currently
// held by an active lease count as allowed. Every other bind() is denied by
// the eBPF program. This closes the "manual process / docker / race" gaps
// because no user-space path can bypass a kernel hook.
//
// `extra` lets the operator whitelist ports the policy does not know about
// (e.g. sshd/22, DNS/53) for a safe first deploy — see README.
func ComputeAllowedPorts(p *Policy, ls *Leases, extra []int) map[uint16]struct{} {
	out := make(map[uint16]struct{})

	add := func(port int) {
		if port >= 0 && port <= 65535 {
			out[uint16(port)] = struct{}{}
		}
	}

	// 1. System-reserved ports (always bindable by their services).
	if p != nil {
		for _, pt := range p.Reserve.BlockedPorts {
			add(pt)
		}
		// The gatekeeper's own listen port is implicitly required.
		add(p.Gatekeeper.ListenPort)
	}

	// 2. Active lease ports (port-bearing leases: kind "port" or "service").
	if ls != nil {
		for i := range ls.Leases {
			l := &ls.Leases[i]
			if (l.Kind == "port" || l.Kind == "service") && l.Port != nil {
				add(*l.Port)
			}
		}
	}

	// 3. Operator extras (safe bootstrap whitelist).
	for _, pt := range extra {
		add(pt)
	}

	return out
}
