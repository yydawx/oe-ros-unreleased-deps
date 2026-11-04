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
- `dist`: RPM dist macro. Use the target distribution, normally `.oe2403`.
- `container_name`: Optional. If omitted, a timestamped name is generated.
- `output_dir`: Optional. Defaults to `/tmp/oe-ros-batch-<timestamp>`.
- `repo_files`: Optional list of host `.repo` files copied into an isolated
  `/etc/yum.repos.d` equivalent. Use this when the image's default mirrors are
  unreachable or a test must pin a particular mirror.
- `dependencies` / `targets`: Ordered arrays.
- `repo` + `branch`: Clone a public Git branch.
- `local_path`: Use a local source tree instead of `repo`/`branch`.
- `spec` or `specs`: Spec paths relative to the source tree.
- `rpmbuild_args`: Optional extra arguments such as `["--without", "tests"]`.
- `builddep_args`: Optional extra `dnf builddep` arguments. `rpmbuild_args`
  conditions such as `--with tests` are translated to matching `-D`
  definitions automatically.
- `test_policy`: `required` by default. Use `allow_empty` only for intentional
  meta packages; use `skip` only when tests are outside the requested scope.
- `spec_test_policies`: Per-spec overrides, for example
  `{"package-meta.spec": "allow_empty"}`.

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
newest result. A failed test policy makes the command exit nonzero even when a
spec incorrectly swallows the test exit code.

The builder reads only from that attempt's `runs/<attempt-id>/sources/` tree,
including when `--reuse-container` is used. A previous run's shared
`/batch/sources/` tree is never used as build input, so local spec, patch, and
source changes are always present in the next attempt.

Every dependency is added to the shared RPM repository and upgrades already
installed packages from that repository before the next item runs.  This is
what prevents an image-provided older package from winning during
`dnf builddep`.

Before adding a rebuilt artifact, the builder removes older RPM files with the
same package name from the shared repository. It also rejects a target that
emits the same non-debug RPM package name from more than one selected spec.

For a multi-spec repository, create one ordered dependency item per
prerequisite spec.  For example, when validating `nav2-route`, place
`nav2-msgs`, `nav2-common`, `nav2-util`, and `nav2-core` before the target.
Do not assume that copying a sibling RPM into the repository makes its headers
available to the next spec.

The reference container script does not delete containers or source trees
automatically. If a build fails, inspect:

```text
<output_dir>/logs/
<output_dir>/rpm/<item>/
```
