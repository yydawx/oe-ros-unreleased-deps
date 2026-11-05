---
name: oe-ros-unreleased-deps
description: "Discover, order, and atomically validate multi-repository openEuler ROS upgrade groups that depend on newer, unpublished source branches. Use with container, QEMU, or host backends; not for production publication."
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
   Automatic discovery can use the merged/open PR window and derive each PR head,
   head repository, base branch, changed files, Maya verdict, and source commit.
   A local source mapping is only an optional reproducibility or dirty-tree override.
2. Check whether each dependency has a source repo or an existing package that is only absent or stale in the public source.
3. Create an ordered, backend-neutral batch plan. Put dependencies before consumers. Record source locators, target specs, build arguments, test policy, architecture, and expected package closure. Start from [references/example-manifest.json](references/example-manifest.json) and see [references/batch-manifest.md](references/batch-manifest.md).

   For a coordinated upgrade cycle, prefer group planning over a hand-written
   manifest:

   ```bash
   python3 scripts/joint_upgrade.py \
     --config group-config.json \
     --output validation-group
   ```

   This discovers the selected PR group, adds dependency-producer PRs needed by
   failed consumers, builds the specs actually changed by each PR, and expands
   all `_multibuild` flavors only when the PR itself changes `_multibuild`.
   It parses internal dependencies from specs and available package manifests,
   detects exact strongly connected cycles and conflicting source variants,
   checks version constraints, and emits a stable topological build order.
   Do not treat every flavor in a repository as part of the PR by default.
   See [references/joint-groups.md](references/joint-groups.md).

   A worked six-PR case study, including the commands, scope reduction,
   dependency follow-ups, targeted retests, and final two-architecture result,
   is in
   [examples/maya-failed-six](examples/maya-failed-six/README.md).
4. Choose a disposable or rollbackable execution backend. Use a container,
   QEMU/VM, remote build worker, or dedicated host according to the target
   architecture and available infrastructure. Read
   [references/execution-backends.md](references/execution-backends.md) and
   record the backend identity, isolation mechanism, architecture, base
   environment, and rollback/reset point.

   Run full x86_64 and aarch64 joint batches sequentially by default. C++
   builds, QEMU translation, package metadata, and debuginfo processing can
   saturate the same host even though the containers appear independent. Start
   another full batch only after measuring spare CPU, memory, disk I/O, and
   container throughput. Do not start duplicate subagents for a batch that is
   already running.

   For a validated `aarch64` QEMU procedure, including cloud-init access,
   overlay setup, guest verification, and source transfer, see
   [references/qemu-aarch64.md](references/qemu-aarch64.md).

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
   builds and installs every spec in each target against that repository. Each
   spec has a separate `rpmbuild` topdir so one spec cannot erase another
   spec's RPMs; duplicate package names are checked across the complete target
   before installation.

   For a container target-only retest, keep the original container running and use
   `--reuse-container` with the same `container_name` and `output_dir`. This
   preserves the existing dependency RPM repository while replacing and
   rebuilding only the revised target.

   If a full-group run is followed by targeted retests, assemble one atomic
   architecture result from the ordered attempt summaries:

   ```bash
   python3 scripts/assemble_group_results.py \
     --plan validation-group/group-plan.json \
     --architecture x86_64 \
     --output validation-group/group-summary-x86_64.json \
     --summary /path/to/full/summary.json \
     --summary /path/to/retest/summary.json
   ```

   Later summaries override only the `group_nodes` they cover. Missing nodes
   remain failed; the command exits nonzero unless the complete plan is green.
   If one spec in a large batch failed, that batch's passing sibling specs stay
   green and only the failed node is replaced by a targeted retest.

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

5. Report build, install, and test results separately. The default developer
   gate is: every affected spec builds and every produced RPM installs. Run
   CTest/pytest and record their parsed status under `test_policy: report`; a
   test failure or missing external fixture is evidence, not a failed RPM
   build. Set `required` only when tests are explicitly part of the requested
   gate. A test-source compilation failure remains a build failure.
6. Preserve the manifest, per-step logs, RPMs, source commit IDs, and final commands in the report.
7. Iterate until the dependency closure is coherent. A successful `dnf builddep` can still resolve an older public package. If compilation cannot find a header, message, service, action, or Python API, compare the public package version with the selected source branch and add the matching same-batch package rather than patching the consumer around the version skew.
8. Run an installation smoke check when the user requests runtime behavior or
   when package usability is the question. Build-and-install validation does
   not by itself prove runtime behavior.
9. Do not infer test success from RPM exit status alone. Many generated specs
   contain `... || echo "RPM TESTS FAILED"`. Parse the CTest/pytest summary so
   the report distinguishes passed, failed, and no-discovered-tests outcomes,
   even when those outcomes are non-blocking.
10. For tests launched with `ros2 test`, prepend the package's `%{buildroot}` prefix and its build tree/test directories to `AMENT_PREFIX_PATH`, `LD_LIBRARY_PATH`, `PATH`, and `PYTHONPATH`. Otherwise launch tests can silently use a previously installed package and fail to find the newly built executables, fixtures, or test-only libraries. The reference runner's `expose_buildroot_in_tests` option overrides the standard make macros with a wrapper so generated ROS specs that source `/opt/ros/setup.sh` still test the RPM currently under construction.
11. Every run writes an isolated attempt record plus a machine-readable summary. Record the plan hash, executor identity (image digest, VM base artifact/QEMU configuration, or host identity), source commit and dirty-tree hash, architecture, per-spec test verdict, and install result. Do not reuse an earlier attempt's logs as current evidence.
    Builds read only from that attempt's source snapshot; never fall back to a shared source tree from an earlier attempt.
12. Reject duplicate RPM names produced by multiple specs in one target and remove stale RPMs of the same package name from the batch-local repository before adding a rebuilt artifact.
13. Evaluate the group atomically. A group is eligible for the merge train only
    when every impacted spec passes. Individual successful PRs do not make the
    group green while another member or shared `_multibuild` flavor is failing.
    The group summary is the control-plane result, not a digest of per-PR runs.

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

Before publishing a change to the planner, run:

```bash
python3 scripts/selftest_joint_upgrade.py
```
