# Joint-upgrade groups

A joint-upgrade group represents all PRs that must become valid together before
any member enters a merge train. Use `scripts/joint_upgrade.py` when the group
must be discovered and ordered rather than assembled by hand.

## Configuration

The minimal configuration contains the API endpoint, PR selectors or discovery
window, source checkout mapping, target distro, test policy, and images:

```json
{
  "group_id": "maya-failures-20261104",
  "cycle_start": "2026-11-01T00:00:00+08:00",
  "target_distro": "humble",
  "dist": ".oe2403",
  "api": {
    "base_url": "https://api.gitcode.com/api/v5",
    "project_owner": "src-openeuler",
    "author": "Rourou2285",
    "maya_author": "wuwei_kubuds",
    "token_file": "~/.config/ag-cli/token.json"
  },
  "repos": ["moveit2", "mavros", "navigation2"],
  "selectors": [
    {"repo": "moveit2", "number": 15},
    {"repo": "mavros", "number": 12}
  ],
  "sources": {
    "moveit2#15": "/path/to/moveit2-pr15",
    "mavros#12": "/path/to/mavros-pr12"
  },
  "default_sources": {
    "moveit2": "/path/to/moveit2-base"
  },
  "pinned_dependencies": [
    {
      "name": "ament-flake8",
      "local_path": "/path/to/ament-lint",
      "specs": ["ament-flake8.spec"],
      "prerequisite_for_all": true,
      "test_policy": "report"
    }
  ],
  "strict_head": true,
  "maya_failed_only": true,
  "test_policy": "report",
  "rpmbuild_args": ["--with", "tests"],
  "images": {
    "x86_64": "oe-ros-builder:24.03",
    "aarch64": "oe-ros-builder:24.03-arm64"
  }
}
```

When `selectors` is omitted, the planner scans open PRs in `repos` for the
configured `author` updated since `cycle_start`. Explicit selectors are useful
for a reproducible replay of an already-identified group.

When a PR source mapping is omitted, the planner materializes the PR head from
GitCode under `<output>/sources/` and verifies the fetched SHA against the API
head SHA. A source mapping can override this for offline replay or an
intentionally dirty local tree. Pinned dependencies may likewise provide
`local_path`, or `repo` plus `branch` (and optionally `head_sha`).

With automatic discovery and `maya_failed_only`, failed PRs are the roots. The
planner walks provider edges backwards, so a passing producer PR is included
when a failed consumer depends on it. Unrelated passing PRs are excluded. The
inverse edges identify consumers of a producer. The resulting root set,
selected PR set, and automatically included dependency PRs are recorded in the
plan's `selection` object. `producer_prs`, `consumer_prs`, and
`pr_relations` expose the cross-PR graph used for merge ordering.

The impact set is intentionally no broader than the PR. Only `.spec` files
changed by the PR are selected. If and only if the PR changes `_multibuild`,
all flavors listed in that file are selected because the build manifest itself
changed. Deleted specs are not rebuilt. Dependencies are pulled into the group
only when a selected consumer requires a package that cannot be satisfied by
the public repository and a same-group source can provide it.

`strict_head` requires each local source checkout to equal the PR head SHA.
Disable it only when the source mapping intentionally represents a frozen
snapshot and record that difference in the report.

Use `pinned_dependencies` for a required local producer that has not been
submitted as a PR yet. `prerequisite_for_all` forces the pinned producer to be
built and installed before every member PR. Record why the pin exists; it is a
group input, not a substitute for filing the producer PR.

## Discovery and planning

```bash
python3 scripts/joint_upgrade.py \
  --config group-config.json \
  --output validation-group \
  --arch x86_64
```

The command writes:

- `group-plan.json`: PRs, Maya verdicts, changed files, the minimal affected
  spec set (`_multibuild` expansion only when that file changed), parsed
  spec/package dependencies, graph edges, topological order (also exposed as
  `merge_order`), exact strongly connected cycles, source conflicts, and
  version/requirement conflicts.
- `batch-x86_64.json`: an atomic `batch_build.py` manifest.

Add `--run` to execute the generated manifest. The runner also writes
`group-summary-x86_64.json`; its `allowed_to_enter_merge_train` value is true
only when every impacted spec for every member PR passes.

## Full impact expansion

Maya and packaging tools can evaluate the entire `_multibuild` repository even
when the visible PR diff changes only one spec. The planner therefore expands a
repository with `_multibuild` to every flavor, not merely the changed spec.

Different PRs in the same repository may carry different one-spec changes. The
planner uses each changed spec's PR checkout and one common checkout for
unchanged flavors. Shared flavor results apply to every PR whose impact set
contains that flavor.

If two PRs propose different source variants for the same repository/spec, the
planner reports a `source_conflicts` entry and refuses to produce a runnable
manifest. This prevents silently choosing one PR's change and calling the
combined group valid.

The same fail-closed treatment applies when selected PRs for one repository
target different base branches or modify the same file. These appear as
`target_branch_conflicts` and `changed_file_conflicts`; they require an explicit
merged source snapshot or a revised PR set.

## Dependency graph and conflicts

Dependencies come from:

- `BuildRequires` and `Requires` in selected specs;
- `package.xml` dependencies found on disk or in the spec's source tarball,
  including `version_gte`, `version_lte`, and `version_eq` attributes;
- package names and versions provided by selected specs.

The planner fails before building when it detects:

- a dependency cycle;
- two selected specs claiming the same package at conflicting versions;
- a selected provider that does not satisfy a declared version constraint.

Missing external dependencies are not assumed to be bugs. They remain normal
`dnf builddep` inputs and are reported by the build attempt if unavailable.
When a dependency has a source repository but is unpublished, represent its
source as a pinned dependency. A dependency with no source repository remains
a new-package blocker.

`spec_test_policies` can override one node by its stable key, for example:

```json
{
  "spec_test_policies": {
    "moveit2:moveit-plugins.spec": "skip"
  }
}
```

Use `skip` only for a proven meta package with no executable or source tests.

## Atomic result

Do not mark a PR green because its own changed spec passed while another shared
flavor failed. The group summary is the control-plane result:

```json
{
  "group_id": "maya-failures-20261104",
  "architecture": "x86_64",
  "status": "failed",
  "allowed_to_enter_merge_train": false,
  "selection": {
    "root_prs": ["moveit2#15"],
    "included_dependency_prs": ["moveit2#14"]
  },
  "prs": {
    "moveit2#15": "failed",
    "moveit2#16": "failed",
    "navigation2#37": "passed"
  },
  "specs": {
    "moveit2:moveit-core.spec": "failed",
    "navigation2:nav2-util.spec": "passed"
  }
}
```

For a multi-architecture gate, retain one group summary per architecture and
combine them:

```bash
python3 scripts/joint_upgrade.py \
  --combine \
    validation-group/group-summary-x86_64.json \
    validation-group/group-summary-aarch64.json \
  --output validation-group
```

The combined `group-summary.json` is green only when every configured
architecture, every member PR, and every impacted spec passes. Combining
summaries with different PR selections is rejected.

When a full attempt is followed by narrow retests, use
`scripts/assemble_group_results.py` as described in `SKILL.md`. It applies
attempts in order at `group_nodes` granularity; later passes can replace earlier
failures, but uncovered nodes remain failed.
