# Batch manifest and reference backend

The validation plan is backend-neutral at the logical level: preserve dependency
order, source locators, specs, build/test conditions, and the expected package
closure when moving between containers, QEMU, or another environment. See
[execution-backends.md](execution-backends.md) for the backend contract.

The JSON below is accepted by the current container reference backend. The
manifest is ordered. Every dependency is built into a shared local RPM
repository before the next item starts. Put an item before every package that
consumes it.

```json
{
  "image": "oe-ros-builder:24.03",
  "dist": ".oe2403",
  "container_name": "oe-ros-batch-demo",
  "output_dir": "/tmp/oe-ros-batch-demo",
  "dependencies": [
    {
      "name": "geographiclib",
      "repo": "https://gitcode.com/src-openeuler/geographiclib.git",
      "branch": "openEuler-24.03-LTS",
      "spec": "GeographicLib.spec"
    }
  ],
  "targets": [
    {
      "name": "mavros",
      "repo": "https://gitcode.com/Rourou2285/mavros.git",
      "branch": "fix-asio-io-context",
      "specs": ["libmavconn.spec", "mavros-msgs.spec", "mavros.spec", "mavros-extras.spec"]
    }
  ]
}
```

Fields:

- `image`: Docker image containing RPM build tools and openEuler repositories.
  Other backends should preserve their equivalent base-environment identity in
  the attempt evidence.
- `platform`: Optional OCI platform such as `linux/amd64` or `linux/arm64`.
- `group_id`: Optional atomic joint-upgrade group identifier.
- `continue_on_error`: When true, collect remaining item results after a
  failure; the final group status still fails if any item failed.
- `dist`: RPM dist macro. Use the target distribution, normally `.oe2403`.
- `target_distro`: ROS distribution, normally `humble`. The builder adds the
  `ros_distro` RPM define to each spec automatically; individual items do not
  need to repeat it.
- `container_name`: Optional. If omitted, a timestamped name is generated.
- `output_dir`: Optional. Defaults to `/tmp/oe-ros-batch-<timestamp>`.
- `repo_files`: Optional list of host `.repo` files copied into an isolated
  `/etc/yum.repos.d` equivalent. Use this when the image's default mirrors are
  unreachable or a test must pin a particular mirror.
- `fixtures`: Optional list of `{ "source": "...", "name": "..." }` objects.
  Files are hashed and copied into `<output_dir>/fixtures/`, mounted in the
  container as `/batch/fixtures/<name>`, and recorded in the attempt summary.
- `setup_scripts`: Optional ordered shell commands run inside the disposable
  backend after fixtures and repository metadata are ready and before any
  package is built.
- `dependencies` / `targets`: Ordered arrays.
- `repo` + `branch`: Clone a public Git branch.
- `local_path`: Use a local source tree instead of `repo`/`branch`.
- `spec` or `specs`: Spec paths relative to the source tree.
- `rpmbuild_args`: Optional extra arguments such as `["--without", "tests"]`.
- `builddep_args`: Optional extra `dnf builddep` arguments. `rpmbuild_args`
  conditions such as `--with tests` are translated to matching `-D`
  definitions automatically.
- `test_policy`: `report` by default for joint developer validation. It runs
  tests and preserves their logs/status but does not block an RPM that built
  and installed. Use `required` only when the user explicitly makes tests a
  release gate, `allow_empty` for an explicit zero-test exception, and `skip`
  when tests are outside the requested scope.
- `spec_test_policies`: Per-spec overrides, for example
  `{"package-meta.spec": "report"}`.
- `expose_buildroot_in_tests`: Defaults to true in generated manifests. It
  installs a make wrapper and overrides `make_build`, `make_install`, and
  `__make` so the current spec's BUILDROOT is prepended to the ROS
  package/library/Python search paths. This keeps `%check` tests from silently
  running against an older package installed in `/opt/ros`. It also assigns a
  stable per-build-directory `ROS_DOMAIN_ID` and localhost-only DDS discovery to
  keep sequential group tests from colliding.
- `group_nodes`: Optional planner metadata mapping a target item to logical spec
  nodes in a joint-upgrade group.

Use `local_path` only for a branch that has already been validated locally. Prefer a pushed branch for CI evidence.
Record the local worktree's `git rev-parse HEAD` in the report because
`local_path` is copied as a source snapshot and does not carry `.git`.

For iterative testing, keep the failed/initial container and rerun a manifest containing only the changed targets:

```bash
python3 scripts/batch_build.py --manifest retest.json --reuse-container
```

The reused container must still be running and its `/batch` mount must match
`output_dir`. Its existing local RPM repository is then used for dependency
resolution. This avoids rebuilding the complete dependency stack after a
target-only spec or source change.

Every invocation creates a new `output_dir/runs/<attempt-id>/` directory.
Logs, source snapshots, `manifest.json`, and `summary.json` for that run never
append to an earlier attempt. `output_dir/latest-summary.json` points to the
newest result. A failed enforced test policy makes the command exit nonzero
even when a spec incorrectly swallows the test exit code. Under `report`, test
failures remain visible in `tests.status` and `tests.evidence` but do not
replace a successful RPM build and installation with a failed group result.
The reference make wrapper applies this only to an explicit `test` or `check`
target after preserving its output; failures from normal `make all`, test-source
compilation, or installation still fail the RPM.

The builder reads only from that attempt's `runs/<attempt-id>/sources/` tree,
including when `--reuse-container` is used. A previous run's shared
`/batch/sources/` tree is never used as build input, so local spec, patch, and
source changes are always present in the next attempt.

Every spec gets a distinct `<attempt>/<item>/<spec>/` `rpmbuild` topdir. Every
dependency is added to the shared RPM repository and upgrades already installed
packages from that repository before the next item runs. This is what prevents
an image-provided older package from winning during `dnf builddep`.

Before adding a rebuilt artifact, the builder removes older RPM files with the
same package name from the shared repository. Before install it checks all
spec topdirs together and rejects a target that emits the same non-debug RPM
package name from more than one selected spec. The final install consumes all
RPMs from all selected spec topdirs, not only the last spec built.

For a multi-spec repository, create one ordered dependency item per
prerequisite spec.  For example, when validating `nav2-route`, place
`nav2-msgs`, `nav2-common`, `nav2-util`, and `nav2-core` before the target.
Do not assume that copying a sibling RPM into the repository makes its headers
available to the next spec.

The reference container script does not delete containers or source trees
automatically. After a target builds and installs, it prunes intermediate
`BUILD`, `BUILDROOT`, and `SOURCES` trees for passing specs to keep long group
runs within disk limits while retaining RPMs and logs. Failed specs keep their
build trees for diagnosis; a global install/duplicate failure keeps all of the
target's build trees. If a build fails, inspect:

```text
<output_dir>/logs/
<output_dir>/rpm/<item>/
```
