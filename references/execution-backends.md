# Execution backends

The correctness of a multi-package upgrade is defined by dependency closure,
build/install/test evidence, and reproducibility. The execution technology is
an implementation choice.

## Backend contract

Before using a backend, confirm that it can:

1. Create an isolated or rollbackable target environment for the requested
   distribution and architecture.
2. Materialize a unique source snapshot for each attempt.
3. Execute non-interactive commands and propagate nonzero exit statuses.
4. Maintain a batch-local RPM repository that can be refreshed and upgraded
   before every dependent build.
5. Install the exact RPMs produced in the current batch.
6. Retrieve logs, RPMs, summaries, and test output after success or failure.
7. Record enough environment provenance to reproduce the result.

Containers are convenient but not required. A backend must not be treated as a
validation oracle merely because it is isolated.

Implement backend mechanics in an adapter. Do not fork or weaken the validation
rules when adding QEMU, remote-worker, or host support, and do not patch package
sources to compensate for backend-specific setup.

## Invariants independent of backend

- Process dependencies before consumers and include unpublished intermediate
  upgrades in the same batch.
- Translate `rpmbuild` conditions such as `--with tests` to the equivalent
  `dnf builddep` definitions.
- Upgrade from the batch repository before each dependent `builddep` and install.
- Remove stale RPMs with the same package name from the batch repository before
  adding a rebuilt artifact.
- Fail when one selected target emits duplicate non-debug RPM package names.
- Read only the current attempt's source snapshot; never reuse a shared source
  directory from an earlier attempt.
- Require explicit CTest/pytest success under the declared test policy. A
  swallowed test command or an empty test run is not a pass unless the spec is
  explicitly marked `allow_empty`.
- Install and, when relevant, exercise the built packages. RPM creation alone
  does not prove installability or runtime compatibility.
- Keep failed attempts for diagnosis and distinguish the latest authoritative
  attempt from superseded logs.

## Evidence envelope

Backends may store logs differently, but the final summary needs equivalent
information:

```json
{
  "attempt_id": "20261104-120000-deadbeef",
  "status": "passed",
  "executor": {
    "kind": "container | qemu | host | remote",
    "identity": "image digest, VM overlay/base ID, or host ID",
    "architecture": "x86_64",
    "distribution": "openEuler-24.03-LTS",
    "isolation": "container | qemu-overlay | disposable-host",
    "reset_point": "image/overlay/snapshot identifier"
  },
  "plan_sha256": "...",
  "sources": [],
  "steps": [],
  "install": {},
  "started_at": "...",
  "finished_at": "...",
  "error": null
}
```

The current Docker reference backend writes an `image` and `image_id`; another
backend should map its equivalent base artifact and runtime configuration into
the same logical evidence.

## QEMU example

A QEMU workflow can use a clean base qcow2 and a per-attempt overlay:

1. Record the base image SHA-256, QEMU arguments, machine type, CPU mode, guest
   architecture, kernel, and distribution.
2. Boot the VM with a writable overlay and expose the attempt workspace through
   virtiofs, 9p, or an equivalent transfer mechanism.
3. Run commands through SSH or a guest agent while preserving exit statuses.
4. Keep the batch-local RPM repository inside the guest or on the shared
   attempt workspace.
5. Retrieve logs and RPMs, then record the overlay/configuration as the
   attempt's environment identity.
6. Discard the overlay for a clean retry, or retain it for diagnosis without
   treating it as a new source snapshot.

For cross-architecture validation, state whether execution is native or
emulated. An emulated build proves buildability in that emulated environment;
do not silently present it as native-performance or production-equivalence
testing.

## Other backends

- **Mock/chroot/build root:** equivalent only if it provides the target OS,
  dependency closure, and isolated batch-local RPM repository.
- **Dedicated host:** use a disposable VM, snapshot, or clearly reversible
  package state. Record the host and repository state; avoid mutating shared
  production repositories.
- **Remote worker:** transfer the immutable attempt snapshot and retrieve all
  evidence. Network location does not remove the ordering and test invariants.
