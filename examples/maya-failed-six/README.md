# Six Maya-failed ROS PRs

This example summarizes a real developer-validation batch for six failed
openEuler ROS PRs. It demonstrates bounded group discovery, RPM build and
install validation, iterative dependency repair, targeted retests, and
two-architecture aggregation.

The goal was not to reproduce a release repository. The goal was to prove that
the PRs can compile and install together against the newest selected source
branches.

## Inputs

| Repository | PR | Changed specs |
|---|---:|---|
| `mavros` | [12](https://gitcode.com/src-openeuler/mavros/merge_requests/12) | `libmavconn.spec` |
| `moveit2` | [15](https://gitcode.com/src-openeuler/moveit2/merge_requests/15) | `moveit-chomp-optimizer-adapter.spec` |
| `moveit2` | [16](https://gitcode.com/src-openeuler/moveit2/merge_requests/16) | `pilz-industrial-motion-planner.spec` |
| `navigation2` | [37](https://gitcode.com/src-openeuler/navigation2/merge_requests/37) | `nav2-constrained-smoother.spec`, `nav2-route.spec` |
| `off_highway_sensor_drivers` | [4](https://gitcode.com/src-openeuler/off_highway_sensor_drivers/merge_requests/4) | 15 existing flavor specs; the deleted umbrella spec was not rebuilt |
| `realsense-ros` | [6](https://gitcode.com/src-openeuler/realsense-ros/merge_requests/6) | `realsense2-camera.spec` |

The initial planner result was:

```text
group=maya-failed-six prs=6 specs=27 runnable=True
```

The 27 nodes comprised 21 changed specs and six unpublished dependency
producers. This bounded scope replaced an earlier over-broad plan that had
expanded every package in every repository.

## Commands

Start from the template and replace the placeholder source paths:

```bash
export GITCODE_TOKEN="<read-only GitCode token>"
cp examples/maya-failed-six/group-config.template.json group-config.json
python3 scripts/joint_upgrade.py \
  --config group-config.json \
  --output validation-group \
  --arch x86_64
python3 scripts/batch_build.py \
  --manifest validation-group/batch-x86_64.json
```

Run a full architecture at a time. Do not start the QEMU aarch64 batch while
the x86_64 batch is still using the same host.

After the x86_64 full batch, two failures required targeted retests:

1. `nav2-route` used `tf2/utils.hpp`, which is absent in openEuler's `tf2`.
   The PR patch covered the goal scorer but omitted the start-pose scorer.
   Extend the patch without disabling tests:

   ```diff
   -#include "tf2/utils.hpp"
   +#include "tf2/utils.h"
   ```

   Retest only `nav2-route.spec` in the retained container. Result: RPM build
   and install passed, 23/23 tests passed.

2. `realsense2-camera` required the new `realsense2_camera_msgs/msg/rgbd`
   interface from its sibling spec. Build only
   `realsense2-camera-msgs.spec`, then retest only
   `realsense2-camera.spec`. Do not add the entire Realsense repository.

Assemble the architecture result after targeted retests:

```bash
python3 scripts/assemble_group_results.py \
  --plan validation-group/group-plan.json \
  --architecture x86_64 \
  --output validation-group/group-summary-x86_64.json \
  --summary <full-attempt>/summary.json \
  --summary <nav2-route-retest>/summary.json \
  --summary <realsense-retest>/summary.json
```

Then run the same flow for `--arch aarch64`. The architecture-specific public
repositories differed: aarch64 still had old `nav2_msgs` and `nav2_core`, so
the aarch64 retest built only `nav2-msgs.spec` and `nav2-core.spec` before
retesting `nav2-route.spec`. `realsense2-camera-msgs.spec` was also built
before retesting `realsense2-camera.spec`.

Combine both architecture summaries:

```bash
python3 scripts/joint_upgrade.py \
  --output validation-group \
  --combine \
  validation-group/group-summary-x86_64.json \
  validation-group/group-summary-aarch64.json
```

## Result

| Architecture | Build | Install | Group result |
|---|---|---|---|
| x86_64 | all 27 selected specs passed | all generated RPMs passed | `passed` |
| aarch64 | all 27 selected specs passed | all generated RPMs passed | `passed` |

All six PRs were `passed` on both architectures after targeted retests.

Tests were run under `test_policy: report`. The aarch64 Realsense build and
install passed while two rosbag visual tests failed; those failures were
retained in evidence and did not invalidate the developer build. This matches
the agreed rule: build or install failures block, while runtime test-case
failures are reported separately.

The individual PR status was:

| PR | x86_64 | aarch64 |
|---|---|---|
| `mavros#12` | passed | passed |
| `moveit2#15` | passed | passed |
| `moveit2#16` | passed | passed |
| `navigation2#37` | passed | passed |
| `off_highway_sensor_drivers#4` | passed | passed |
| `realsense-ros#6` | passed | passed |

Here, `passed` means the selected group passed local RPM build and install
validation; it does not mean Maya was rerun or that the PRs were merged or
published.

## What to reuse

- Start from changed spec files, not every flavor in the repository.
- Expand `_multibuild` only when the PR changes `_multibuild`.
- Add a sibling producer only after the consumer proves it needs the newer API.
- Retest the failed node in the same container and local RPM repository.
- Keep architectures serial by default.
- Summarize at group level; do not call a partial retest a Maya pass.
- Preserve failed test evidence even when it is non-blocking.
