# Follow-up dependencies found during execution

These packages were not initial PR roots. Add them only when a selected
consumer proves that the public package is too old or lacks the required
interface.

| Consumer | Missing producer | Why it was needed |
|---|---|---|
| `realsense2-camera.spec` | `realsense2-camera-msgs.spec` | Adds the `rgbd` message used by the camera implementation |
| `nav2-route.spec` on aarch64 | `nav2-msgs.spec` | Public aarch64 package lacks route messages |
| `nav2-route.spec` on aarch64 | `nav2-core.spec` | Public aarch64 package lacks `route_exceptions.hpp` |

Build these as ordered dependencies before the failed target, then retest only
the target and assemble the new summary over the original full-batch result.
