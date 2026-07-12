# mcp-gatekeeper / eBPF — kernel-level bind() enforcement (ADR-0055, Фаза 3)

eBPF **LSM `socket_bind`** program that denies `bind()` on any TCP/UDP port
that is **not** in a kernel allowlist. The allowlist is maintained from
user-space by the Go loader and combines:

1. `reserve.blocked_ports` from `policy_v1.yaml` — system-reserved ports
   (PostgreSQL/5432, node_exporter/9100, the gatekeeper itself/8888, …) that
   legitimate services must still be able to bind.
2. **Active leases** from `data/leases.json` — ports agents have actually
   registered through `mcp-gatekeeper` (kind `port` / `service`).
3. Operator extras (`--allow-extra`, `--allow-current-listeners`).

Anything else (manual `python3 server.py`, Docker-published ports, a race
window after a lease expiry) is blocked **at the kernel**, with no user-space
path to bypass. This closes ADR-0055 gaps **2, 3, 6, 9**.

---

## ⚠️ Kernel prerequisites (verify BEFORE enabling enforce)

```bash
# 1. Kernel >= 5.7 (we run 5.15 — OK)
uname -r

# 2. BPF LSM compiled in
grep BPF_LSM /boot/config-$(uname -r)     # must be =y (zcat /proc/config.gz if present)

# 3. bpf present in the active LSM stack (CRITICAL, usually MISSING).
#    This box currently runs: landlock,lockdown,yama,integrity,apparmor
#    (see CONFIG_LSM in /boot/config-$(uname -r)). `bpf` is NOT there,
#    so an LSM_BPF attach will FAIL and the loader fail-open (nothing blocked).
cat /proc/cmdline | tr ' ' '\n' | grep '^lsm='
```

If `lsm=bpf` is **not** present, the LSM hook will fail to attach and the
loader will **fail-open** (exit, nothing blocked). To enable:

```bash
# add `bpf` to the LSM list — APPEND it, do NOT replace the existing
# stack (otherwise you would silently disable AppArmor/yama/landlock!):
#   current: lsm=landlock,lockdown,yama,integrity,apparmor
#   desired: lsm=landlock,lockdown,yama,integrity,apparmor,bpf
sed -i 's/^GRUB_CMDLINE_LINUX="/&lsm=landlock,lockdown,yama,integrity,apparmor,bpf /' /etc/default/grub
update-grub && reboot
```

> Do **NOT** edit the cmdline without coordination — a reboot is required and
> must be scheduled. The eBPF code is written and tested; **loading on the
> live box is deferred to the coordinator** (per task rules).

---

## Build

Requires `clang` (BPF target), `libbpf-dev`, kernel headers, and Go 1.21+.

```bash
cd mcp-gatekeeper/ebpf
make            # builds bind.o (eBPF) + gatekeeper-ebpf (Go loader)
# or separately:
make ebpf      # clang -> bind.o
make go        # go build -> gatekeeper-ebpf
```

`bind.o` is loaded from (in order): `--bpf-object`, `$GATEKEEPER_EBPF_O`,
`./bind.o`, `/usr/local/lib/gatekeeper/bind.o`.

---

## Run (modes)

### 1) Audit mode first (RECOMMENDED first deploy)

Counts and **logs** every bind that *would* be denied, but allows it. Watch
the counter to discover any legit service you forgot to whitelist:

```bash
./gatekeeper-ebpf --mode audit --allow-current-listeners \
    --policy ../policies/policy_v1.yaml --leases ../data/leases.json
# wait, watch journal: "... audit counter (binds on non-allowed ports): N"
```

If `N` only grows for ports you expect to be blocked → safe to enforce.

### 2) Enforce mode

```bash
./gatekeeper-ebpf --mode enforce --allow-current-listeners
```

`--allow-current-listeners` seeds the allowlist with every port already
`LISTEN`-ing (from `/proc/net/tcp{,6}`) so an existing `sshd`/22 etc. is not
cut off. **Safety guard:** running `--mode=enforce` *without* either
`--allow-current-listeners` or `--enforce-ack` is refused (the loader exits
with an explanatory message) — this prevents accidentally bricking SSH.

`--enforce-ack` is for operators who have verified the allowlist by hand.

### Updating the map

The loader re-reads `leases.json` + policy every `--reload-interval` (default
5s) and reconciles the BPF map: new leases are added, expired/removed leases
are deleted, reserved ports stay. **No restart needed** when leases change.

---

## Fail-open guarantee

If anything prevents loading/attaching the eBPF program — missing `bind.o`,
no `CAP_BPF`/`CAP_SYS_ADMIN`, kernel lacks `lsm=bpf`, verifier reject — the
loader **logs the error and exits**. Because the LSM hook is never attached,
**nothing is blocked**: the box keeps running, just without kernel
enforcement. systemd's `Restart=on-failure` + `StartLimitBurst=3` then stop
retrying. This is intentional: a broken enforcer must never become a
system-wide firewall that locks you out.

---

## systemd

```bash
make install                       # installs bind.o, binary, unit
sudo systemctl daemon-reload
sudo systemctl enable --now gatekeeper-ebpf
sudo systemctl status gatekeeper-ebpf
journalctl -u gatekeeper-ebpf -f
```

The unit runs `--mode enforce --allow-current-listeners`, needs
`CAP_BPF`+`CAP_SYS_ADMIN`, and is `Restart=on-failure`.

---

## Files

| File | Purpose |
|------|---------|
| `bind.c` | eBPF LSM `socket_bind` program (deny non-allowlisted ports) |
| `policy.go` | pure logic: load policy/leases, compute allowlist (unit-tested) |
| `matcher.go` | `PortMap` interface + in-memory mock (for tests) + reconcile |
| `loader.go` | cilium/ebpf load/attach/populate the real BPF maps |
| `main.go` | flags, fail-open, audit/enforce, watch loop, signal handling |
| `ebpf_test.go` | unit tests — mock map, allowed/denied logic (NO live kernel) |
| `Makefile` | build `bind.o` + `gatekeeper-ebpf` |
| `gatekeeper-ebpf.service` | systemd unit |

---

## Tests

```bash
gofmt -l .      # should print nothing (or fix with: gofmt -w .)
go vet ./...
go test ./... -v
```

`go test` exercises `ComputeAllowedPorts` and the `reconcile`/mock-map logic
in plain user-space. It **never loads eBPF** and never touches the live
kernel — safe to run anywhere.

---

## Known limitations

- **Reload race:** leases.json is polled (default 5s). Between an agent
  registering a port and the loader reconciling, a bind could be denied. The
  agent registers *before* binding, and the window is seconds; acceptable for
  Фаза 3. A future improvement is server→loader push via a local control
  socket for instant updates.
- **Audit counter** (`settings[1]`) is monotonic; pair with the journal.
- IPv4/IPv6 only; AF_UNIX/AF_NETLINK are always allowed.
