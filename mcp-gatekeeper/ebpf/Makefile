# Makefile — build the eBPF object + Go loader for mcp-gatekeeper (ADR-0055 Фаза 3)
#
# Prerequisites (build host):
#   - clang (with BPF target: `clang --print-supported-cpus | grep bpf`)
#   - libbpf headers  (apt: libbpf-dev ; the `bpf/bpf_helpers.h`)
#   - kernel headers   (apt: linux-headers-$(uname -r) ; provides linux/*.h)
#   - Go 1.21+        (only for the loader binary)
#
# Kernel runtime prerequisites (target host):
#   - Linux >= 5.7 with CONFIG_BPF_LSM=y
#   - `lsm=bpf` on the kernel cmdline (check: `cat /proc/cmdline`)
#     Add via grub: GRUB_CMDLINE_LINUX="... lsm=bpf" && update-grub && reboot

CLANG      ?= clang
BPF_TARGET ?= bpf
ARCH       ?= $(shell uname -m | sed 's/x86_64/x86/;s/aarch64/arm64/')
BPF_CFLAGS ?= -O2 -g -target $(BPF_TARGET) -D__TARGET_ARCH_$(ARCH)
INCLUDES   ?= -I/usr/include -I/usr/include/bpf -I/usr/include/$(shell uname -m | sed 's/x86_64/x86_64-linux-gnu/;s/aarch64/aarch64-linux-gnu/;s/armv7l/arm-linux-gnueabihf/')

OBJ    := bpf/bind.o
BIN    := gatekeeper-ebpf
PKG    := .

.PHONY: all build ebpf go vet test fmt clean install

all: ebpf go

# --- eBPF object ---
ebpf: $(OBJ)

$(OBJ): bpf/bind.c
	$(CLANG) $(BPF_CFLAGS) $(INCLUDES) -c $< -o $@

# --- Go loader ---
go:
	go build -o $(BIN) $(PKG)

build: ebpf go

vet:
	go vet ./...

test:
	go test ./... -v

fmt:
	gofmt -w .

# Install: object next to the binary so the loader finds it by default.
install: build
	install -d /usr/local/lib/gatekeeper
	install -m 0644 $(OBJ) /usr/local/lib/gatekeeper/bind.o
	install -m 0755 $(BIN) /usr/local/bin/$(BIN)
	install -m 0644 gatekeeper-ebpf.service /etc/systemd/system/gatekeeper-ebpf.service

clean:
	rm -f $(OBJ) $(BIN)
