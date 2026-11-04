# openEuler RPM release model

Use these state transitions when diagnosing a missing dependency:

```text
source branch and commit
  -> buildable RPM
  -> release-management project membership
  -> EulerMaker project build
  -> release branch promotion
  -> public repodata
```

Merging a source PR only advances the first stage. Maya validates a candidate source tree against published dependencies. It does not publish packages.

For ROS humble on 24.03, the normal branch chain is:

```text
humble
  -> Multi-Version_ros-humble_openEuler-24.03-LTS-Next
  -> Multi-Version_ros-humble_openEuler-24.03-LTS
```

The release project expects source package names such as `geometry2`, `common_interfaces`, and `librealsense`, not only their binary names (`tf2`, `geometry-msgs`, `librealsense2`).

Before declaring a package unpublished:

1. Check the public OS, Everything, EPOL, update, and ROS multi-version repositories.
2. Check the expected package name, provides, and exact version.
3. Check the source repository and all relevant branches.
4. Check `openeuler/release-management` `pckg-mgmt.yaml`.
5. Distinguish no source repository from source-ready-but-not-released.

If source packages already exist and only the binary is missing, this skill should build a temporary validation batch. Production publication remains a separate maintainer action.
