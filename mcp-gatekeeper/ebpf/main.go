// Command gatekeeper-ebpf — userspace loader/controller for the mcp-gatekeeper
// eBPF LSM socket_bind enforcement (ADR-0055, Фаза 3).
//
// It reads the policy (reserve.blocked_ports), the active leases (leases.json)
// and any operator extras, computes the kernel allowlist, and keeps the eBPF
// map in sync. Design goals:
//
//   - Kernel-level enforcement: no user-space path can bypass a bind() hook.
//   - Fail-open: if the eBPF object cannot be loaded/attached, we log and
//     EXIT (the hook is never attached, so NOTHING is blocked) instead of
//     risking a bricked box.
//   - Safe rollout: default is AUDIT mode (count + allow). Switching to
//     ENFORCE requires a conscious operator action (--allow-current-listeners
//     or --enforce-ack) so you never accidentally deny sshd/22 etc.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	defaultPolicy = "/root/LabDoctorM/projects/mcp-tools/mcp-gatekeeper/policies/policy_v1.yaml"
	defaultLeases = "/root/LabDoctorM/projects/mcp-tools/mcp-gatekeeper/data/leases.json"
)

// leasesDefault resolves the leases.json path: honour $GATEKEEPER_DATA (a
// directory, set by mcp-gatekeeper.service) by appending leases.json, else a
// repo-relative default.
func leasesDefault() string {
	if d := os.Getenv("GATEKEEPER_DATA"); d != "" {
		return strings.TrimRight(d, "/") + "/leases.json"
	}
	return defaultLeases
}

func main() {
	var (
		policyPath  = flag.String("policy", envOr("GATEKEEPER_POLICY", defaultPolicy), "path to policy_v1.yaml")
		leasesPath  = flag.String("leases", leasesDefault(), "path to leases.json (default: $GATEKEEPER_DATA/leases.json or repo data/)")
		objFlag     = flag.String("bpf-object", "", "path to compiled bind.o (else $GATEKEEPER_EBPF_O, ./bind.o, /usr/local/lib/gatekeeper/bind.o)")
		mode        = flag.String("mode", "enforce", "enforcement mode: 'enforce' (deny) or 'audit' (count+allow)")
		allowExtra  = flag.String("allow-extra", "", "comma-separated extra ports always allowed (e.g. 22,53)")
		allowListen = flag.Bool("allow-current-listeners", false, "seed allowlist with currently-listening ports (safe first deploy)")
		enforceAck  = flag.Bool("enforce-ack", false, "acknowledge the allowlist is complete; required to run --mode=enforce without --allow-current-listeners")
		reloadInt   = flag.Duration("reload-interval", 5*time.Second, "how often to re-read policy/leases and reconcile the map")
		metricsInt  = flag.Duration("metrics-interval", 60*time.Second, "how often to log the audit/denied counter")
	)
	flag.Parse()

	enforce := *mode == "enforce"
	if !enforce && *mode != "audit" {
		log.Fatalf("invalid --mode %q (want 'enforce' or 'audit')", *mode)
	}

	// --- Safety guard: never enforce with an unknown/incomplete allowlist ---
	if enforce && !*allowListen && !*enforceAck {
		log.Fatal("REFUSING to run in enforce mode: the kernel allowlist would not " +
			"include ports outside policy+leases+extra. Re-run with " +
			"--allow-current-listeners (seed currently-listening ports incl. sshd) " +
			"or --enforce-ack if you have verified the allowlist is complete.")
	}

	// --- Build the operator extras (explicit + current listeners) ---
	extra, err := parsePorts(*allowExtra)
	if err != nil {
		log.Fatalf("parse --allow-extra: %v", err)
	}
	if *allowListen {
		live, lerr := currentListeningPorts()
		if lerr != nil {
			log.Printf("WARN cannot read current listeners: %v", lerr)
		} else {
			extra = append(extra, live...)
			log.Printf("seeded allowlist with %d currently-listening port(s)", len(live))
		}
	}

	// --- Resolve the eBPF object path ---
	objPath := resolveObjPath(*objFlag)
	if !fileExists(objPath) {
		// Fail-open: without a compiled object we can't enforce, but we must
		// NOT block anything. Exit so systemd stops retrying (no hook attached).
		log.Fatalf("eBPF object %q not found — nothing will be enforced (fail-open). "+
			"Build it with `make` (needs clang + libbpf + kernel headers).", objPath)
	}

	// --- Load policy + leases (leases missing is non-fatal) ---
	policy, perr := LoadPolicy(*policyPath)
	if perr != nil {
		log.Fatalf("load policy: %v", perr)
	}
	leases, lerr := LoadLeases(*leasesPath)
	if lerr != nil {
		log.Printf("WARN load leases %q: %v (enforcing reserved-port baseline only)", *leasesPath, lerr)
		leases = &Leases{}
	}

	allowed := ComputeAllowedPorts(policy, leases, extra)
	log.Printf("computed allowlist: %d port(s) (enforce=%v)", len(allowed), enforce)

	// --- Load + attach (fail-open on any error) ---
	loader, err := NewLoader(objPath, enforce)
	if err != nil {
		log.Fatalf("LOAD FAILED (fail-open, nothing blocked): %v", err)
	}
	// Populate BEFORE attach so there is no empty-window where all binds deny.
	if err := loader.Reconcile(allowed); err != nil {
		loader.Close()
		log.Fatalf("populate allowlist failed (fail-open): %v", err)
	}
	if err := loader.Attach(); err != nil {
		loader.Close()
		log.Fatalf("ATTACH FAILED (fail-open, nothing blocked): %v", err)
	}
	defer loader.Close()
	log.Printf("eBPF LSM socket_bind ACTIVE — mode=%s", *mode)

	// --- Watch / periodic reconcile loop ---
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)

	tick := time.NewTicker(*reloadInt)
	defer tick.Stop()
	met := time.NewTicker(*metricsInt)
	defer met.Stop()

	for {
		select {
		case <-stop:
			log.Printf("received stop signal, detaching eBPF and exiting")
			return
		case <-met.C:
			if c, cerr := loader.DeniedCount(); cerr == nil {
				log.Printf("audit counter (binds on non-allowed ports): %d", c)
			}
		case <-tick.C:
			ls, rerr := LoadLeases(*leasesPath)
			if rerr != nil {
				log.Printf("WARN reloading leases failed, keeping last allowlist: %v", rerr)
				continue
			}
			cur := ComputeAllowedPorts(policy, ls, extra)
			if err := loader.Reconcile(cur); err != nil {
				log.Printf("WARN reconcile failed: %v", err)
				continue
			}
			log.Printf("reconciled allowlist: %d port(s)", len(cur))
		}
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func fileExists(p string) bool {
	info, err := os.Stat(p)
	return err == nil && !info.IsDir()
}

func resolveObjPath(flag string) string {
	candidates := []string{}
	if flag != "" {
		candidates = append(candidates, flag)
	}
	if e := os.Getenv("GATEKEEPER_EBPF_O"); e != "" {
		candidates = append(candidates, e)
	}
	candidates = append(candidates, "./bpf/bind.o", "./bind.o", "/usr/local/lib/gatekeeper/bind.o")
	for _, c := range candidates {
		if c != "" && fileExists(c) {
			return c
		}
	}
	if len(candidates) > 0 {
		return candidates[0]
	}
	return "bind.o"
}

func parsePorts(s string) ([]int, error) {
	var out []int
	s = strings.TrimSpace(s)
	if s == "" {
		return out, nil
	}
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		p, err := strconv.Atoi(part)
		if err != nil {
			return nil, fmt.Errorf("bad port %q: %w", part, err)
		}
		if p < 0 || p > 65535 {
			return nil, fmt.Errorf("port %d out of range", p)
		}
		out = append(out, p)
	}
	return out, nil
}

// currentListeningPorts parses /proc/net/tcp{,6} for sockets in LISTEN state
// and returns their local ports. Used by --allow-current-listeners so the very
// first enforce run does not deny already-running services (sshd, etc.).
func currentListeningPorts() ([]int, error) {
	var ports []int
	for _, f := range []string{"/proc/net/tcp", "/proc/net/tcp6"} {
		data, err := os.ReadFile(f)
		if err != nil {
			continue // file may be absent (e.g. no IPv6)
		}
		for i, line := range strings.Split(string(data), "\n") {
			if i == 0 {
				continue // header
			}
			flds := strings.Fields(line)
			if len(flds) < 4 {
				continue
			}
			addrPort := flds[1] // "IP:PORT" (hex, host order)
			idx := strings.LastIndex(addrPort, ":")
			if idx < 0 {
				continue
			}
			portHex := addrPort[idx+1:]
			if flds[3] != "0A" { // 0A = TCP_LISTEN
				continue
			}
			p, err := strconv.ParseUint(portHex, 16, 32)
			if err != nil {
				continue
			}
			ports = append(ports, int(p))
		}
	}
	return ports, nil
}
