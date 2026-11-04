---
name: oe-ros-unreleased-deps
description: "Validate multi-package openEuler ROS upgrades whose BuildRequires need newer, unpublished source branches. Use with any suitable isolated execution backend, including containers, QEMU VMs, or dedicated hosts; not for production repository publication."
---

# openEuler ROS Unreleased Dependencies

Use this skill when a ROS RPM PR cannot resolve a dependency because the dependency's source branch is newer than the binary package in the public openEuler repository.

The skill defines the validation protocol, not the execution technology. A
container, QEMU virtual machine, remote builder, or dedicated host can supply
the build environment as long as it preserves the same inputs, evidence, and
acceptance criteria. The Docker script in `scripts/` is one reference backend.

## Decide the boundary first

Do not treat these states as equivalent:

- **Source available**: a correct branch exists in `src-openeuler`.
- **Binary available**: `dnf repoquery` finds the required package and version.
- **Source PR passed Maya**: Maya built the isolated MR source.
- **Product released**: the RPM is in the target public repository's `repodata`.

For developer validation, satisfy the dependency from RPMs built from the selected source branches. Do not wait for those RPMs to enter the public product repository.

## Workflow

1. Identify the target repository, branch, specs, missing package names, and minimum versions.
2. Check whether each dependency has a source repo or an existing package that is only absent or stale in the public source.
3. Create an ordered, backend-neutral batch plan. Put dependencies before consumers. Record source locators, target specs, build arguments, test policy, architecture, and expected package closure. Start from [references/example-manifest.json](references/example-manifest.json) and see [references/batch-manifest.md](references/batch-manifest.md).
4. Choose a disposable or rollbackable execution backend. Use a container,
   QEMU/VM, remote build worker, or dedicated host according to the target
   architecture and available infrastructure. Read
   [references/execution-backends.md](references/execution-backends.md) and
   record the backend identity, isolation mechanism, architecture, base
   environment, and rollback/reset point.

   Do not change the validation semantics when changing backends. The backend
   must provide command execution, source upload, a persistent batch-local RPM
   repository, DNF resolution, log/artifact retrieval, and enough environment
   provenance to reproduce the attempt.

   ### Container reference backend

   When the chosen backend is Docker-compatible, run the reference builder:

   ```bash
   python3 scripts/batch_build.py --manifest batch.json
   ```

   Use `--dry-run` first to inspect the plan. The script creates a disposable
   build container, builds each dependency into a local RPM repository, then
   builds and installs each target spec against that repository.

   For a container target-only retest, keep the original container running and use
   `--reuse-container` with the same `container_name` and `output_dir`. This
   preserves the existing dependency RPM repository while replacing and
   rebuilding only the revised target.

   Before every later `dnf builddep` and target install, the builder upgrades
   installed packages from the batch repository.  This is required: `dnf
   builddep` otherwise keeps a newer-by-release local RPM uninstalled and can
   compile the consumer against an older header set already present in the
   image.  Treat the local repository as the newest layer, not just an
   additional source for missing package names.

   When several specs in one source repository depend on each other, put the
   prerequisites in `dependencies` as separate ordered items before the final
   `targets`.  Copying a built RPM into the local repository is not enough for
   a sibling spec to see its headers; a following `builddep` must resolve and
   install it.

5. Report build, install, and test results separately. A non-blocking upstream test failure is not the same as a failed RPM build, and missing external test data is not a code failure. Enforce the plan's `test_policy`; by default each spec must emit a non-empty passing CTest/pytest summary.
6. Preserve the manifest, per-step logs, RPMs, source commit IDs, and final commands in the report.
7. Iterate until the dependency closure is coherent. A successful `dnf builddep` can still resolve an older public package. If compilation cannot find a header, message, service, action, or Python API, compare the public package version with the selected source branch and add the matching same-batch package rather than patching the consumer around the version skew.
8. Treat runtime entry points as part of validation. For Python/CLI packages, check at least one import or help/command surface after installation; an installable package can still be unusable against an older package from the public repository.
9. When the request requires tests to pass, do not infer success from RPM exit status alone. Many generated specs contain `... || echo "RPM TESTS FAILED"`. Inspect the CTest/pytest summary, require zero failed tests, and distinguish a test with no discovered cases from a test that was skipped or swallowed.
10. For tests launched with `ros2 test`, prepend the package's `%{buildroot}` prefix and its build tree/test directories to `AMENT_PREFIX_PATH`, `LD_LIBRARY_PATH`, `PATH`, and `PYTHONPATH`. Otherwise launch tests can silently use a previously installed package and fail to find the newly built executables, fixtures, or test-only libraries.
11. Every run writes an isolated attempt record plus a machine-readable summary. Record the plan hash, executor identity (image digest, VM base artifact/QEMU configuration, or host identity), source commit and dirty-tree hash, architecture, per-spec test verdict, and install result. Do not reuse an earlier attempt's logs as current evidence.
    Builds read only from that attempt's source snapshot; never fall back to a shared source tree from an earlier attempt.
12. Reject duplicate RPM names produced by multiple specs in one target and remove stale RPMs of the same package name from the batch-local repository before adding a rebuilt artifact.

## Hard constraints

- Never add network downloads to an RPM spec to obtain a dependency. Put required source tarballs in `SourceN` or use the batch local repository.
- Never treat an unrelated package's bundled header as a stable dependency.
- Vendor only header-only libraries, and only after checking package ownership, include paths, license, and duplicate-file behavior.
- For shared libraries and ROS message/interface packages, build the real package and use a local repository. Do not copy their source into every consumer.
- Keep the build isolated from production and the user's normal development
  environment. Use a disposable environment, snapshot/overlay, or explicitly
  reversible setup; QEMU and dedicated hosts are valid when they provide the
  same guarantees. A failed run must leave logs and artifacts available for
  diagnosis.
- Do not claim a target is validated against the new dependency merely because
  `builddep` exited successfully. Confirm the installed EVR (`rpm -q` or the
  install log) and inspect the compiler include path when header/API changes
  are the subject of the validation.
- If the manifest requires a package with no source repository, stop that branch and report it as requiring a new package. Check the upstream tarball, its normal dependencies, license, architecture support, and whether the openEuler repository genuinely does not exist.
- Do not claim success from `rpmbuild` alone when the requested validation includes installation or runtime behavior.

## Classification hints

- Public source version older than the required source branch: build that dependency from source first.
- Source repository exists but has no target branch: add/build the missing target branch before dependent validation.
- No `src-openeuler` repository exists: this is a new-package task, not a local dependency workaround.
- Missing dependency belongs to another PR: include that source branch in the same manifest, even if its MR is not merged or published.
- ABI-breaking core dependency (`rcl`, `rclcpp`, `tf2`, message interfaces): build the complete affected batch and do not vendor it.
- Public repository has the package but test fixtures fail to download: record the network failure separately from build success.
- One source repository may contain both a current per-package spec and a stale umbrella spec. Before validating, list the `Name:` values produced by every selected spec. Do not allow two specs in the same batch to emit the same package name with different versions; fix or exclude the stale spec explicitly and record that decision.
- Test-only `BuildRequires` may appear even when `%check` is effectively disabled. Build the same-source test-support packages first, or change the test bcond in a temporary verified copy rather than forcing unrelated public packages into the batch.

See [references/openEuler-rpm-release-model.md](references/openEuler-rpm-release-model.md) for the distinction between source branches, Maya, EulerMaker, release-management, and public repositories.

## Final report

Report a compact matrix containing:

| Item | Source branch/commit | RPM result | Install result | Blocker |
|---|---|---|---|---|

Include the exact missing-package names and versions. State whether the result proves only source-level buildability or also installation/runtime behavior.
