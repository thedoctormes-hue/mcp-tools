// SPDX-License-Identifier: GPL-2.0
//
// bind.c — eBPF LSM `socket_bind` enforcement for mcp-gatekeeper (ADR-0055 Фаза 3)
//
// Kernel-level firewall for bind(): any process (agent, docker, manual python
// server, race-window) that tries to bind() a TCP/UDP port which is NOT in the
// BPF `allowed_ports` map is denied with -EPERM. The allowlist is maintained
// from user-space by the Go loader (reserve.blocked_ports + active leases from
// leases.json + operator extras).
//
// Compile (see ../Makefile):
//   clang -O2 -g -target bpf -D__TARGET_ARCH_x86 \
//          -I/usr/include -I/usr/include/bpf -I/usr/include/x86_64-linux-gnu \
//          -c bind.c -o bind.o
//
// Requires: kernel >= 5.7 with CONFIG_BPF_LSM=y and `lsm=bpf` on the cmdline.
//
// NOTE: we define the minimal sockaddr structs ourselves instead of pulling in
// the full UAPI <linux/socket.h>/<linux/in.h> (which only forward-declare them
// for BPF). This keeps the build dependency-free (just libbpf-dev + kernel
// UAPI headers) and portable.

#include <linux/bpf.h>
#include <linux/errno.h>
#include <bpf/bpf_helpers.h>

// Minimal socket address types (matched to the kernel UAPI layout).
typedef __u16 sa_family_t;

struct sockaddr {
	sa_family_t sa_family;
	char sa_data[14];
};

struct sockaddr_in {
	sa_family_t sin_family;
	__u16 sin_port;
	struct { __u32 s_addr; } sin_addr;
	char sin_zero[8];
};

struct sockaddr_in6 {
	sa_family_t sin6_family;
	__u16 sin6_port;
	__u32 sin6_flowinfo;
	struct { __u8 s6_addr[16]; } sin6_addr;
	__u32 sin6_scope_id;
};

char LICENSE[] SEC("license") = "GPL";

#ifndef AF_INET
#define AF_INET 2
#endif
#ifndef AF_INET6
#define AF_INET6 10
#endif

// allowed_ports: key = host-order TCP/UDP port (u16); presence (value != 0)
// means "this port may be bound". Absence => deny (in enforce mode).
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, __u16);
	__type(value, __u8);
	__uint(max_entries, 65536);
} allowed_ports SEC(".maps");

// settings[0] = enforce flag: 1 => deny non-allowed binds, 0 => audit only
//                                (allow but count would-be denials).
// settings[1] = counter: number of bind attempts on non-allowed ports.
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, __u64);
	__uint(max_entries, 2);
} settings SEC(".maps");

static __always_inline int port_from_sockaddr(struct sockaddr *addr, __u16 *port)
{
	__u16 family = addr->sa_family;

	if (family == AF_INET) {
		struct sockaddr_in *a = (struct sockaddr_in *)addr;
		*port = __builtin_bswap16(a->sin_port);
		return 1;
	} else if (family == AF_INET6) {
		struct sockaddr_in6 *a = (struct sockaddr_in6 *)addr;
		*port = __builtin_bswap16(a->sin6_port);
		return 1;
	}
	// Non-IP families (AF_UNIX, AF_NETLINK, ...) are out of scope: allow.
	return 0;
}

SEC("lsm/socket_bind")
int restrict_socket_bind(struct socket *sock, struct sockaddr *address, int addrlen)
{
	__u16 port = 0;

	if (!port_from_sockaddr(address, &port))
		return 0;

	// Allowlisted port -> permit immediately.
	__u8 *allowed = bpf_map_lookup_elem(&allowed_ports, &port);
	if (allowed)
		return 0;

	// Not in the allowlist: count the attempt regardless of mode.
	__u32 ckey = 1;
	__u64 *cnt = bpf_map_lookup_elem(&settings, &ckey);
	if (cnt)
		__sync_fetch_and_add(cnt, 1);

	// Enforce mode: actually deny.
	__u32 ekey = 0;
	__u64 *enf = bpf_map_lookup_elem(&settings, &ekey);
	if (enf && *enf == 1)
		return -EPERM;

	// Audit mode (default / fail-open): allow but it was counted above.
	return 0;
}
