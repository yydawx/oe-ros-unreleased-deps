#!/usr/bin/env python3
"""Reference Docker backend for ordered openEuler ROS joint-upgrade validation.

The validation rules and evidence contract live in ``SKILL.md`` and
``references/execution-backends.md``. This script is only one implementation;
QEMU, VM, remote-worker, and dedicated-host backends can implement the same
protocol.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CTEST_PASS = re.compile(r"\b\d+% tests passed, 0 tests failed out of [1-9]\d*\b")
PYTEST_PASS = re.compile(r"(?m)^=+\s+[1-9]\d* passed(?:, \d+ skipped)?(?:, \d+ warnings?)? in ")
PYTEST_FAIL = re.compile(r"(?m)^=+\s+.*\b(?:failed|errors?)\b.*\bin ")
PYTEST_EMPTY = re.compile(r"(?m)(?:collected 0 items|no tests ran in )")
FAILURE_MARKERS = (
    "RPM TESTS FAILED",
    "The following tests FAILED:",
    "Errors while running CTest",
)
MAKE_WRAPPER = r"""#!/bin/bash
# Expose the RPM currently under test before any installed public package.
# Some generated ROS specs source /opt/ros/setup.sh in %check, which otherwise
# hides the package's own BUILDROOT from package.xml and ament-index lookups.
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-$((100 + $(printf %s "$PWD" | cksum | cut -d' ' -f1) % 100))}
export ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY:-1}
probe=$PWD
buildroot=
while [[ $probe != / ]]; do
  if [[ -d $probe/BUILDROOT ]]; then
    buildroot=$(find "$probe/BUILDROOT" -mindepth 1 -maxdepth 1 -type d -print -quit)
    [[ -n $buildroot ]] && break
  fi
  probe=$(dirname "$probe")
done
prefix=
if [[ -n $buildroot ]]; then
  prefix=$(find "$buildroot/opt/ros" -mindepth 1 -maxdepth 1 -type d -print -quit 2>/dev/null || true)
fi
if [[ -n $prefix ]]; then
  export AMENT_PREFIX_PATH=$prefix${AMENT_PREFIX_PATH:+:$AMENT_PREFIX_PATH}
  export CMAKE_PREFIX_PATH=$prefix${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}
  export COLCON_PREFIX_PATH=$prefix${COLCON_PREFIX_PATH:+:$COLCON_PREFIX_PATH}
  export LD_LIBRARY_PATH=$prefix/lib:$prefix/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
  export PATH=$prefix/bin:$PATH
  for python_site in "$prefix"/lib/python*/site-packages "$prefix"/lib64/python*/site-packages; do
    [[ -d $python_site ]] && PYTHONPATH=$python_site${PYTHONPATH:+:$PYTHONPATH}
  done
  export PYTHONPATH
fi
test_target=0
for argument in "$@"; do
  case "$argument" in
    test|check)
      test_target=1
      ;;
  esac
done
/usr/bin/make "$@"
status=$?
if [[ $test_target -eq 1 && $status -ne 0 && ${OE_ROS_TEST_POLICY:-required} == report ]]; then
  echo "OE_ROS_TEST_POLICY=report: CTest/pytest failure is recorded but non-blocking" >&2
  exit 0
fi
exit $status
"""


def die(message: str) -> "NoReturn":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def checked(value: str, field: str) -> str:
    if not SAFE_NAME.fullmatch(value):
        die(f"{field} must match {SAFE_NAME.pattern}: {value!r}")
    return value


def run(
    command: list[str], *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    print("+", shlex.join(command), flush=True)
    result = subprocess.run(command, text=True, capture_output=capture)
    if check and result.returncode != 0:
        die(f"command failed with exit status {result.returncode}")
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    ignored = {".git", "RPMS", "BUILD", "BUILDROOT"}
    for path in sorted(root.rglob("*")):
        if any(part in ignored for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256_file(path).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def assess_test_log(log_path: Path, policy: str) -> dict[str, Any]:
    if policy not in {"required", "allow_empty", "skip", "report"}:
        die(f"test_policy must be required, allow_empty, skip, or report: {policy!r}")
    if policy == "skip":
        return {"policy": policy, "status": "skipped", "enforced": False}
    if not log_path.is_file():
        die(f"test log does not exist: {log_path}")

    text = log_path.read_text(encoding="utf-8", errors="replace")
    no_tests = (
        "No tests were found!!!" in text
        or "RPM TESTS SKIPPED" in text
        or bool(PYTEST_EMPTY.search(text))
    )
    if (
        policy == "allow_empty"
        and no_tests
        and "[  FAILED  ]" not in text
        and not re.search(r"\b[1-9]\d* tests failed\b", text)
    ):
        return {
            "policy": policy,
            "status": "empty_allowed",
            "enforced": policy != "report",
            "evidence": "no tests found",
        }
    failures = [line.strip() for line in text.splitlines() if any(m in line for m in FAILURE_MARKERS)]
    failures.extend(match.group(0).strip() for match in PYTEST_FAIL.finditer(text))
    if failures:
        unique = list(dict.fromkeys(failures))
        return {
            "policy": policy,
            "status": "failed",
            "enforced": policy != "report",
            "evidence": unique[-20:],
        }

    ctest_passed = CTEST_PASS.search(text)
    pytest_passed = PYTEST_PASS.search(text)
    if ctest_passed or pytest_passed:
        return {
            "policy": policy,
            "status": "passed",
            "enforced": policy != "report",
            "evidence": (ctest_passed or pytest_passed).group(0).strip(),
        }
    if policy == "allow_empty" and no_tests:
        return {
            "policy": policy,
            "status": "empty_allowed",
            "enforced": True,
            "evidence": "no tests found",
        }
    return {
        "policy": policy,
        "status": "failed",
        "enforced": policy != "report",
        "evidence": ["no passing CTest/pytest summary found"],
    }


def rpmbuild_args_for_builddep(args: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"--with", "--without"} and index + 1 < len(args):
            prefix = "_with_" if value == "--with" else "_without_"
            result.extend(["-D", f"{prefix}{args[index + 1]} 1"])
            index += 2
            continue
        if value == "--define" and index + 1 < len(args):
            result.extend(["-D", args[index + 1]])
            index += 2
            continue
        if value.startswith("--define="):
            result.extend(["-D", value.split("=", 1)[1]])
            index += 1
            continue
        result.append(value)
        index += 1
    return result


def container_exec(name: str, script: str, log_path: Path, verbose: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = ["docker", "exec", name, "bash", "-lc", script]
    print("+", shlex.join(command), flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                log.write(line)
                if verbose:
                    print(line, end="")
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            raise
        status = proc.wait()
    if status:
        tail = "".join(log_path.read_text(errors="replace").splitlines(True)[-120:])
        print(tail, file=sys.stderr)
        die(f"step failed; full log: {log_path}")


def container_capture(name: str, script: str, log_path: Path) -> str:
    result = run(
        ["docker", "exec", name, "bash", "-lc", script],
        check=False,
        capture=True,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(result.stdout or "", encoding="utf-8")
    if result.returncode:
        die(f"capture step failed; full log: {log_path}")
    return (result.stdout or "").strip()


def require(item: dict, key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        die(f"missing string field {key!r} in {item!r}")
    return value


def clone_or_copy(
    item: dict,
    name: str,
    work: Path,
    output: Path,
    container: str,
) -> tuple[str, dict[str, Any]]:
    # Build from the immutable source snapshot retained in this attempt.  The
    # output directory is mounted at /batch, so this also gives every local and
    # remote source tree a unique guest path instead of reusing a shared tree.
    guest = f"/batch/{work.relative_to(output)}/sources/{name}"
    local_path = item.get("local_path")
    if local_path:
        source = Path(local_path).expanduser().resolve()
        if not source.is_dir():
            die(f"local_path is not a directory: {source}")
        # A remote dependency built earlier is cloned inside the container as
        # root.  Return the shared source tree to the invoking host user before
        # shutil copies a local_path into it.
        destination = work / "sources" / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(".git", "RPMS", "BUILD", "BUILDROOT"),
        )
        commit = run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=False,
            capture=True,
        )
        status = run(
            ["git", "-C", str(source), "status", "--porcelain=v1"],
            check=False,
            capture=True,
        )
        diff = run(
            ["git", "-C", str(source), "diff", "--binary", "HEAD"],
            check=False,
            capture=True,
        )
        return guest, {
            "type": "local_path",
            "path": str(source),
            "commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
            "status": status.stdout.splitlines() if status.returncode == 0 else [],
            "diff_sha256": hashlib.sha256(diff.stdout.encode()).hexdigest()
            if diff.returncode == 0
            else None,
            "tree_sha256": tree_sha256(destination),
        }

    repo = require(item, "repo")
    branch = require(item, "branch")
    script = (
        "set -euo pipefail; "
        f"mkdir -p {shlex.quote(str(Path(guest).parent))}; "
        f"rm -rf {shlex.quote(guest)}; "
        "export GIT_TERMINAL_PROMPT=0; "
        "for attempt in 1 2 3 4 5; do "
        f"git clone --depth 1 --branch {shlex.quote(branch)} "
        f"{shlex.quote(repo)} {shlex.quote(guest)} && break; "
        "sleep 2; "
        "done; "
        f"test -d {shlex.quote(guest)}/.git"
    )
    container_exec(container, script, work / "logs" / f"{name}-clone.log", False)
    commit = container_capture(
        container,
        f"git -C {shlex.quote(guest)} rev-parse HEAD",
        work / "logs" / f"{name}-source-head.log",
    )
    return guest, {
        "type": "git",
        "repo": repo,
        "branch": branch,
        "commit": commit,
    }


def build_item(
    item: dict,
    kind: str,
    work: Path,
    output: Path,
    container: str,
    dist: str,
    target_distro: str | None,
    dnf_options: list[str],
    verbose: bool,
    continue_on_error: bool,
    attempt_id: str,
) -> dict[str, Any]:
    name = checked(require(item, "name"), "name")
    source, provenance = clone_or_copy(item, name, work, output, container)
    specs = item.get("specs")
    if specs is None:
        specs = [require(item, "spec")]
    if not isinstance(specs, list) or not specs or not all(isinstance(x, str) for x in specs):
        die(f"{name}: specs must be a non-empty string array")
    test_policy = item.get("test_policy", "report")
    if test_policy not in {"required", "allow_empty", "skip", "report"}:
        die(f"{name}: test_policy must be required, allow_empty, skip, or report")
    spec_test_policies = item.get("spec_test_policies", {})
    if not isinstance(spec_test_policies, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in spec_test_policies.items()
    ):
        die(f"{name}: spec_test_policies must be an object of strings")

    attempt_build_root = f"/batch/rpm/{attempt_id}"
    item_root = f"{attempt_build_root}/{name}"
    setup = (
        "set -euo pipefail; "
        f"rm -rf {shlex.quote(item_root)}; "
        f"mkdir -p {shlex.quote(item_root)}; "
    )
    container_exec(container, setup, work / "logs" / f"{name}-prepare.log", verbose)

    rpmbuild_args = item.get("rpmbuild_args", [])
    if not isinstance(rpmbuild_args, list) or not all(isinstance(x, str) for x in rpmbuild_args):
        die(f"{name}: rpmbuild_args must be a string array")
    builddep_args = item.get("builddep_args", [])
    if not isinstance(builddep_args, list) or not all(isinstance(x, str) for x in builddep_args):
        die(f"{name}: builddep_args must be a string array")
    expose_buildroot = item.get("expose_buildroot_in_tests", True)
    if not isinstance(expose_buildroot, bool):
        die(f"{name}: expose_buildroot_in_tests must be a boolean")
    make_wrapper = f"{attempt_build_root}/helpers/oe-ros-make"
    if expose_buildroot:
        encoded_wrapper = base64.b64encode(MAKE_WRAPPER.encode()).decode()
        wrapper_setup = (
            "set -euo pipefail; "
            f"mkdir -p {shlex.quote(str(Path(make_wrapper).parent))}; "
            f"printf %s {shlex.quote(encoded_wrapper)} | base64 -d "
            f"> {shlex.quote(make_wrapper)}; "
            f"chmod 0755 {shlex.quote(make_wrapper)}"
        )
        container_exec(
            container,
            wrapper_setup,
            work / "logs" / f"{name}-test-wrapper.log",
            verbose,
        )

    result: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "status": "passed",
        "source": provenance,
        "specs": [],
        "build_root": attempt_build_root,
    }
    spec_failures: list[dict[str, str]] = []
    spec_topdirs: list[str] = []

    for spec in specs:
        if "/" in spec or spec.startswith("."):
            die(f"{name}: spec must be a relative filename: {spec!r}")
        spec_guest = f"{source}/{spec}"
        log_name = f"{name}-{Path(spec).stem}.log"
        log_path = work / "logs" / log_name
        spec_topdir = f"{item_root}/{Path(spec).stem}"
        spec_topdirs.append(spec_topdir)
        effective_rpmbuild_args = list(rpmbuild_args)
        if target_distro and not any(
            value == "ros_distro" or value.startswith("ros_distro ")
            for value in effective_rpmbuild_args
        ):
            effective_rpmbuild_args.extend(["--define", f"ros_distro {target_distro}"])
        effective_builddep_args = list(builddep_args)
        effective_builddep_args.extend(
            rpmbuild_args_for_builddep(effective_rpmbuild_args)
        )
        spec_setup = (
            "set -euo pipefail; "
            f"mkdir -p {shlex.quote(spec_topdir)}/{{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}}; "
            f"cp -a {shlex.quote(source)}/. {shlex.quote(spec_topdir)}/SOURCES/; "
            f"cp {shlex.quote(spec_guest)} {shlex.quote(spec_topdir)}/SPECS/"
        )
        container_exec(
            container,
            spec_setup,
            work / "logs" / f"{name}-{Path(spec).stem}-prepare.log",
            verbose,
        )
        command = " ".join(
            [
                f"export OE_ROS_TEST_POLICY={shlex.quote(spec_test_policies.get(spec, test_policy))};",
                "rpmbuild -ba",
                f"--define {shlex.quote('_smp_mflags -j4')}",
                f"--define {shlex.quote('_topdir ' + spec_topdir)}",
                f"--define {shlex.quote('dist ' + dist)}",
                *(
                    [
                        f"--define {shlex.quote('__make ' + make_wrapper)}",
                        f"--define {shlex.quote('make_build ' + make_wrapper + ' -O -j${RPM_BUILD_NCPUS} V=1 VERBOSE=1')}",
                        f"--define {shlex.quote('make_install ' + make_wrapper + ' install DESTDIR=${RPM_BUILD_ROOT} INSTALL=\"/usr/bin/install -p\"')}",
                    ]
                    if expose_buildroot
                    else []
                ),
                *map(shlex.quote, effective_rpmbuild_args),
                shlex.quote(spec_guest),
            ]
        )
        script = (
            "set -euo pipefail; "
            "if ! command -v createrepo_c >/dev/null 2>&1; then "
            f"dnf -y {' '.join(map(shlex.quote, dnf_options))} install createrepo_c; fi; "
            "mkdir -p /batch/repo; "
            "[ -e /batch/repo/repodata/repomd.xml ] || createrepo_c /batch/repo; "
            f"dnf -q {' '.join(map(shlex.quote, dnf_options))} "
            "--repofrompath=oe-batch,file:///batch/repo "
            "--setopt=oe-batch.gpgcheck=0 "
            "--setopt=oe-batch.repo_gpgcheck=0 "
            "--setopt=oe-batch.metadata_expire=0 "
            "--repo=oe-batch upgrade -y; "
            f"dnf -q {' '.join(map(shlex.quote, dnf_options))} "
            "--repofrompath=oe-batch,file:///batch/repo "
            "--setopt=oe-batch.gpgcheck=0 "
            "--setopt=oe-batch.repo_gpgcheck=0 "
            "--setopt=oe-batch.metadata_expire=0 "
            f"builddep -y {' '.join(map(shlex.quote, effective_builddep_args))} "
            f"{shlex.quote(spec_guest)}; "
            f"{command}; "
            "duplicates=$(find "
            f"{shlex.quote(spec_topdir)}/RPMS -type f -name '*.rpm' "
            "! -name '*-debuginfo-*' ! -name '*-debugsource-*' -print0 "
            "| xargs -0 -r rpm -qp --qf '%{NAME}\\n' "
            "| sort | uniq -d); "
            "if [ -n \"$duplicates\" ]; then "
            "echo 'duplicate RPM package names in one target:'; "
            "echo \"$duplicates\"; exit 40; fi; "
            "while IFS= read -r -d '' rpm; do "
            "package_name=$(rpm -qp --qf '%{NAME}' \"$rpm\"); "
            "find /batch/repo -maxdepth 1 -type f "
            "-name \"$package_name-*.rpm\" -delete; "
            "cp -f \"$rpm\" /batch/repo/; "
            "done < <(find "
            f"{shlex.quote(spec_topdir)}/RPMS -type f -name '*.rpm' "
            "! -name '*-debuginfo-*' ! -name '*-debugsource-*' -print0); "
            "createrepo_c --update /batch/repo"
        )
        try:
            container_exec(container, script, log_path, verbose)
        except SystemExit as error:
            failure = {
                "spec": spec,
                "log": str(log_path),
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
            result["specs"].append(failure)
            spec_failures.append(failure)
            if not continue_on_error:
                raise
            continue
        policy = spec_test_policies.get(spec, test_policy)
        assessment = assess_test_log(log_path, policy)
        spec_result = {"spec": spec, "log": str(log_path), "tests": assessment}
        if assessment["status"] == "failed" and policy != "report":
            failure = {
                **spec_result,
                "status": "failed",
                "error": "test policy failed: "
                + "; ".join(assessment.get("evidence", [])),
            }
            result["specs"].append(failure)
            spec_failures.append(failure)
            if not continue_on_error:
                die(f"{name}/{spec}: {failure['error']}")
        else:
            result["specs"].append(spec_result)

    if kind == "target":
        duplicate_log = work / "logs" / f"{name}-duplicates.log"
        duplicate_script = (
            "set -euo pipefail; "
            "duplicates=$(find "
            f"{' '.join(shlex.quote(value) for value in spec_topdirs)} "
            "-type f -path '*/RPMS/*' -name '*.rpm' "
            "! -name '*-debuginfo-*' ! -name '*-debugsource-*' -print0 "
            "| xargs -0 -r rpm -qp --qf '%{NAME}\\n' "
            "| sort | uniq -d); "
            "if [ -n \"$duplicates\" ]; then "
            "echo 'duplicate RPM package names across target specs:'; "
            "echo \"$duplicates\"; exit 40; fi"
        )
        try:
            container_exec(container, duplicate_script, duplicate_log, verbose)
            result["duplicate_check"] = {
                "status": "passed",
                "log": str(duplicate_log),
            }
        except SystemExit as error:
            failure = {
                "stage": "duplicate-check",
                "log": str(duplicate_log),
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
            spec_failures.append(failure)
            result["duplicate_check"] = failure
        install_script = (
            "set -euo pipefail; "
            "mapfile -d '' rpms < <(find "
            f"{' '.join(shlex.quote(value) for value in spec_topdirs)} "
            "-type f -path '*/RPMS/*' -name '*.rpm' "
            "! -name '*-debuginfo-*' ! -name '*-debugsource-*' -print0); "
            "[ ${#rpms[@]} -gt 0 ]; "
            f"dnf -y {' '.join(map(shlex.quote, dnf_options))} "
            "--repofrompath=oe-batch,file:///batch/repo "
            "--setopt=oe-batch.gpgcheck=0 "
            "--setopt=oe-batch.repo_gpgcheck=0 "
            "--setopt=oe-batch.metadata_expire=0 "
            "--repo=oe-batch upgrade -y; "
            f"dnf -y {' '.join(map(shlex.quote, dnf_options))} "
            "--repofrompath=oe-batch,file:///batch/repo "
            "--setopt=oe-batch.gpgcheck=0 "
            "--setopt=oe-batch.repo_gpgcheck=0 "
            "--setopt=oe-batch.metadata_expire=0 "
            "install \"${rpms[@]}\"; "
            "printf 'installed RPMs:\\n'; "
            "printf '  %s\\n' \"${rpms[@]}\""
        )
        install_log = work / "logs" / f"{name}-install.log"
        try:
            container_exec(container, install_script, install_log, verbose)
            result["install"] = {
                "status": "passed",
                "log": str(install_log),
                "spec_topdirs": spec_topdirs,
            }
        except SystemExit as error:
            failure = {
                "stage": "install",
                "log": str(install_log),
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
            spec_failures.append(failure)
            result["install"] = failure
        global_stage_failed = any(
            failure.get("stage") in {"duplicate-check", "install"}
            for failure in spec_failures
        )
        failed_specs = {
            failure["spec"]
            for failure in spec_failures
            if isinstance(failure.get("spec"), str)
        }
        prune_topdirs = []
        if not global_stage_failed:
            for spec in specs:
                if spec not in failed_specs:
                    prune_topdirs.append(f"{item_root}/{Path(spec).stem}")
        if prune_topdirs:
            prune_log = work / "logs" / f"{name}-prune.log"
            prune_script = (
                "set -euo pipefail; "
                + " ".join(
                    f"rm -rf {shlex.quote(topdir)}/BUILD "
                    f"{shlex.quote(topdir)}/BUILDROOT "
                    f"{shlex.quote(topdir)}/SOURCES;"
                    for topdir in prune_topdirs
                )
            )
            container_exec(container, prune_script, prune_log, verbose)
            result["pruned_build_trees"] = {
                "status": "passed",
                "topdirs": prune_topdirs,
                "log": str(prune_log),
            }
    if spec_failures:
        result["status"] = "failed"
        result["errors"] = spec_failures
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reuse-container",
        action="store_true",
        help="Reuse the manifest's running container and its /batch local RPM repository.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    image = require(manifest, "image")
    platform = manifest.get("platform")
    if platform is not None and (
        not isinstance(platform, str) or not re.fullmatch(r"linux/[A-Za-z0-9_]+", platform)
    ):
        die(f"platform must look like linux/arm64: {platform!r}")
    dist = manifest.get("dist", ".oe2403")
    target_distro = manifest.get("target_distro")
    if target_distro is not None and (
        not isinstance(target_distro, str) or not SAFE_NAME.fullmatch(target_distro)
    ):
        die(f"target_distro must match {SAFE_NAME.pattern}: {target_distro!r}")
    group_id = manifest.get("group_id")
    if group_id is not None and (
        not isinstance(group_id, str) or not SAFE_NAME.fullmatch(group_id)
    ):
        die(f"group_id must match {SAFE_NAME.pattern}: {group_id!r}")
    continue_on_error = manifest.get("continue_on_error", False)
    if not isinstance(continue_on_error, bool):
        die("continue_on_error must be a boolean")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    attempt_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    container = manifest.get("container_name") or f"oe-ros-batch-{attempt_id}"
    checked(container, "container_name")
    output = Path(manifest.get("output_dir") or f"/tmp/oe-ros-batch-{stamp}").expanduser().resolve()
    if output == Path("/") or output == Path.home().resolve():
        die(f"unsafe output_dir: {output}")
    work = output / "runs" / attempt_id
    dependencies = manifest.get("dependencies", [])
    targets = manifest.get("targets", [])
    if not isinstance(dependencies, list) or not isinstance(targets, list):
        die("dependencies and targets must be arrays")
    if not targets:
        die("manifest contains no targets")
    fixtures = manifest.get("fixtures", [])
    if not isinstance(fixtures, list):
        die("fixtures must be an array")
    setup_scripts = manifest.get("setup_scripts", [])
    if not isinstance(setup_scripts, list) or not all(
        isinstance(value, str) and value for value in setup_scripts
    ):
        die("setup_scripts must be a string array")

    plan = [
        *(("dependency", item) for item in dependencies),
        *(("target", item) for item in targets),
    ]
    print(f"image: {image}")
    print(f"output: {output}")
    print(f"attempt: {attempt_id}")
    print("build order:")
    for kind, item in plan:
        print(f"  {kind:10} {item.get('name', '<missing>')}")
    if args.dry_run:
        return

    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=False)
    manifest_copy = work / "manifest.json"
    shutil.copy2(args.manifest, manifest_copy)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "status": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": sha256_file(args.manifest),
        "image": image,
        "image_id": None,
        "dist": dist,
        "target_distro": target_distro,
        "group_id": group_id,
        "platform": platform,
        "container": container,
        "output_dir": str(output),
        "fixtures": [],
        "items": [],
    }
    summary_path = work / "summary.json"

    def persist_summary() -> None:
        write_json_atomic(summary_path, summary)
        write_json_atomic(output / "latest-summary.json", summary)

    persist_summary()
    dnf_options = [
        "--setopt=install_weak_deps=False",
        "--setopt=timeout=30",
        "--setopt=retries=3",
    ]
    repo_files = manifest.get("repo_files", [])
    if not isinstance(repo_files, list) or not all(isinstance(x, str) for x in repo_files):
        die("repo_files must be a string array")
    if repo_files:
        repo_dir = output / "repo-config"
        repo_dir.mkdir(exist_ok=True)
        for value in repo_files:
            source = Path(value).expanduser().resolve()
            if not source.is_file():
                die(f"repo file does not exist: {source}")
            shutil.copy2(source, repo_dir / source.name)
        dnf_options.append("--setopt=reposdir=/batch/repo-config")

    if args.reuse_container:
        running = run(
            ["docker", "inspect", container, "--format", "{{.State.Running}}"],
            check=False,
            capture=True,
        )
        if running.returncode != 0 or running.stdout.strip() != "true":
            die(f"container is not running: {container}")
        mounted_output = run(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{range .Mounts}}{{if eq .Destination "/batch"}}{{.Source}}{{end}}{{end}}',
            ],
            check=False,
            capture=True,
        )
        mounted_path = mounted_output.stdout.strip()
        if mounted_output.returncode != 0 or not mounted_path or Path(mounted_path).resolve() != output:
            die(f"container {container} is not mounted at expected output: {output}")
    else:
        docker_run = ["docker", "run", "-d", "--name", container]
        if platform:
            docker_run.extend(["--platform", platform])
        docker_run.extend(["-v", f"{output}:/batch", image, "sleep", "infinity"])
        run(docker_run)
    image_id = run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        check=False,
        capture=True,
    )
    if image_id.returncode != 0:
        die(f"image not found: {image}")
    summary["image_id"] = image_id.stdout.strip()
    fixture_dir = output / "fixtures"
    fixture_names: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            die(f"invalid fixture: {fixture!r}")
        source = Path(require(fixture, "source")).expanduser().resolve()
        if not source.is_file():
            die(f"fixture is not a file: {source}")
        name = fixture.get("name") or source.name
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            die(f"fixture name must match {SAFE_NAME.pattern}: {name!r}")
        if name in fixture_names:
            die(f"duplicate fixture name: {name}")
        fixture_names.add(name)
        fixture_dir.mkdir(parents=True, exist_ok=True)
        destination = fixture_dir / name
        shutil.copy2(source, destination)
        summary["fixtures"].append(
            {
                "source": str(source),
                "guest_path": f"/batch/fixtures/{name}",
                "sha256": sha256_file(destination),
            }
        )
    persist_summary()
    failures: list[dict[str, str]] = []
    try:
        container_exec(
            container,
            "dnf -q --refresh " + " ".join(map(shlex.quote, dnf_options)) + " makecache",
            work / "logs" / "dnf-makecache.log",
            args.verbose,
        )
        if setup_scripts:
            container_exec(
                container,
                "set -euo pipefail; " + "\n".join(setup_scripts),
                work / "logs" / "setup.log",
                args.verbose,
            )
        for kind, item in plan:
            try:
                result = build_item(
                    item,
                    kind,
                    work,
                    output,
                    container,
                    dist,
                target_distro,
                dnf_options,
                args.verbose,
                continue_on_error,
                attempt_id,
            )
            except (Exception, SystemExit) as error:
                failure = {
                    "kind": kind,
                    "name": str(item.get("name", "<missing>")),
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                }
                summary["items"].append(failure)
                failures.append(failure)
                persist_summary()
                if not continue_on_error:
                    raise
                continue
            summary["items"].append(result)
            if result.get("status") == "failed":
                failures.append(result)
                if not continue_on_error:
                    raise SystemExit(f"item failed: {result['name']}")
            persist_summary()
    except BaseException as error:
        summary["status"] = "failed"
        summary["error"] = f"{type(error).__name__}: {error}"
        summary["finished_at"] = utc_now()
        persist_summary()
        print(f"container retained for diagnosis: {container}", file=sys.stderr)
        raise
    summary["status"] = "failed" if failures else "passed"
    if failures:
        summary["errors"] = failures
    summary["finished_at"] = utc_now()
    persist_summary()
    print(f"batch completed: {work}")
    print(f"summary: {summary_path}")
    print(f"container retained: {container}")
    if failures:
        die(f"{len(failures)} item(s) failed in group {group_id or '<none>'}")


if __name__ == "__main__":
    main()
