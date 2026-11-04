# Tested aarch64 QEMU backend

This runbook was validated on an x86_64 host with QEMU TCG against openEuler
24.03 LTS AArch64. It proves buildability in an emulated AArch64 environment;
it is not a native-performance test.

## Prepare the host

Install `qemu-system-aarch64`, `qemu-img`, AArch64 EDK2 firmware,
`cloud-image-utils`, and an SSH client. If the host does not permit system
package installation, run these tools from an isolated helper environment.

Download the official VM image and verify its published checksum:

```bash
base_url=https://repo.openeuler.org/openEuler-24.03-LTS/virtual_machine_img/aarch64
curl -fL --continue-at - -o openEuler-24.03-LTS-aarch64.qcow2.xz \
  "$base_url/openEuler-24.03-LTS-aarch64.qcow2.xz"
curl -fL -o openEuler-24.03-LTS-aarch64.qcow2.xz.sha256sum \
  "$base_url/openEuler-24.03-LTS-aarch64.qcow2.xz.sha256sum"
sha256sum -c openEuler-24.03-LTS-aarch64.qcow2.xz.sha256sum
xz -dk openEuler-24.03-LTS-aarch64.qcow2.xz
```

Create one overlay per attempt:

```bash
qemu-img create -f qcow2 -F qcow2 \
  -b openEuler-24.03-LTS-aarch64.qcow2 arm-root.qcow2 40G
```

## Inject access

Create a cloud-init NoCloud seed with an SSH key for a non-root user in the
`wheel` group and passwordless sudo. Build the seed with:

```bash
cloud-localds seed.iso user-data meta-data
```

Keep `user-data` and the private key outside the published repository.

## Boot

The following command was tested with QEMU using TCG:

```bash
qemu-system-aarch64 \
  -machine virt,accel=tcg,highmem=on \
  -cpu max -smp 4 -m 6144 \
  -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  -drive if=virtio,format=qcow2,file=arm-root.qcow2 \
  -drive if=virtio,format=raw,readonly=on,file=seed.iso \
  -device virtio-rng-pci \
  -device virtio-net-pci,netdev=net0 \
  -netdev user,id=net0,hostfwd=tcp:0.0.0.0:2222-:22 \
  -nographic
```

If QEMU reports a missing `efi-virtio.rom`, install the distribution's
`ipxe-qemu` package or equivalent firmware package.

Record the base image checksum, QEMU version and arguments, firmware checksum,
CPU mode (`tcg`), guest architecture, kernel, distribution, and overlay path.

## Verify the guest before building

```bash
ssh -p 2222 oe@127.0.0.1 '
  uname -m
  uname -r
  cat /etc/os-release
  getconf LONG_BIT
'
```

`uname -m` must report `aarch64`. Configure the target OS and ROS repositories,
then verify that public dependencies resolve on that architecture.

## Run the same validation protocol

1. Materialize a unique source snapshot inside the guest attempt directory.
2. Run `dnf builddep` with the same `--with`, `--without`, and `--define`
   conditions passed to `rpmbuild`.
3. Build the prerequisite with `rpmbuild`.
4. Parse CTest or pytest output rather than trusting the shell exit status.
5. Add only the new RPMs to the batch-local repository and run `createrepo_c`.
6. Resolve the consumer through all configured repositories so the local
   higher-release EVR wins while public dependencies remain available.
7. Install both RPMs, verify `rpm -q` architecture and EVR, and exercise an
   import or runtime entry point.

Do not use `dnf --repo=<batch-repo>` for the final install unless the local
repository is intentionally self-contained; it can hide valid dependencies in
the public repositories.

## Cross-architecture regression lessons

- A test command used by `%check` must have an explicit `BuildRequires`.
  Preinstalled tools in an x86 builder image are not architecture-independent
  guarantees. This validation found missing `python3-pytest` and
  `python3-flake8` declarations in `ament-flake8`.
- When sourcing an ament `setup.sh` under `set -u`, temporarily disable nounset;
  older setup scripts reference unset trace variables.
- Preserve failed attempts. A missing tool in a minimal guest is valid evidence
  that the spec has an undeclared build dependency.
