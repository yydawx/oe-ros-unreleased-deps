#!/usr/bin/env python3
"""Discover and execute an atomic multi-repository ROS upgrade group.

The planner uses GitCode PR metadata to discover a group, expands every touched
``_multibuild`` repository to its complete flavor set, derives an internal
dependency graph, and writes a manifest for ``batch_build.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEP_RE = re.compile(
    r"^(BuildRequires|Requires)(?:\([^)]*\))?\s*:\s*(.+)$",
    re.MULTILINE,
)
NAME_RE = re.compile(r"^Name\s*:\s*(\S+)", re.MULTILINE)
VERSION_RE = re.compile(r"^Version\s*:\s*(\S+)", re.MULTILINE)
DEFINE_RE = re.compile(r"^%(?:define|global)\s+(\w+)\s+(.+)$", re.MULTILINE)
SOURCE_RE = re.compile(r"^Source\d*\s*:\s*(\S+)", re.MULTILINE)
PROVIDES_RE = re.compile(r"^Provides\s*:\s*(.+)$", re.MULTILINE)
PACKAGE_RE = re.compile(
    r"^%package(?P<named>\s+-n)?\s+(?P<value>\S+)",
    re.MULTILINE,
)
CONSTRAINT_RE = re.compile(r"^(\S+)\s*(>=|<=|=|>|<)?\s*(.*)$")


def die(message: str) -> "NoReturn":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        die(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def token_from(config: dict[str, Any]) -> str | None:
    api = config.get("api", {})
    token_file = api.get("token_file")
    if not token_file:
        return os.environ.get("GITCODE_TOKEN") or None
    path = Path(str(token_file)).expanduser()
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("access_token"), str):
        return data["access_token"]
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        return data["accounts"][0]["access_token"]
    die(f"cannot read token from {path}")


def api_get(config: dict[str, Any], endpoint: str) -> Any:
    api = config.get("api", {})
    base = api.get("base_url", "https://api.gitcode.com/api/v5").rstrip("/")
    headers = {"Accept": "application/json"}
    token = token_from(config)
    if token:
        headers["PRIVATE-TOKEN"] = token
    last_error: Exception | None = None
    for attempt in range(10):
        try:
            request = urllib.request.Request(base + endpoint, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(min(1 + attempt, 5))
    die(f"GitCode API failed for {endpoint}: {last_error}")


def api_get_list(config: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
    separator = "&" if "?" in endpoint else "?"
    result: list[dict[str, Any]] = []
    for page in range(1, 101):
        payload = api_get(config, f"{endpoint}{separator}per_page=100&page={page}")
        if not isinstance(payload, list):
            die(f"GitCode API returned a non-list for {endpoint}: {type(payload).__name__}")
        result.extend(value for value in payload if isinstance(value, dict))
        if len(payload) < 100:
            return result
    die(f"GitCode API pagination exceeded 100 pages for {endpoint}")


def discover_prs(config: dict[str, Any]) -> list[dict[str, Any]]:
    api = config.get("api", {})
    owner = api.get("project_owner", "src-openeuler")
    author = api.get("author")
    cycle_start = config.get("cycle_start")
    selectors = config.get("selectors")
    selected: list[tuple[str, int]] = []
    if selectors is not None:
        if not isinstance(selectors, list):
            die("selectors must be an array")
        for value in selectors:
            if not isinstance(value, dict) or not isinstance(value.get("repo"), str):
                die(f"invalid selector: {value!r}")
            selected.append((value["repo"], int(value["number"])))
    else:
        if not author or not cycle_start:
            die("automatic discovery requires api.author and cycle_start")
        since = datetime.fromisoformat(str(cycle_start).replace("Z", "+00:00"))
        repos = config.get("repos")
        if not isinstance(repos, list) or not all(isinstance(x, str) for x in repos):
            die("repos must be a string array for automatic discovery")
        for repo in repos:
            pulls = api_get_list(
                config,
                f"/repos/{owner}/{repo}/pulls?state=open",
            )
            for pull in pulls:
                user = pull.get("user") or {}
                updated = pull.get("updated_at") or ""
                try:
                    updated_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if user.get("login") == author and updated_at >= since:
                    selected.append((repo, int(pull["number"])))

    result: list[dict[str, Any]] = []
    for repo, number in sorted(set(selected)):
        detail = api_get(config, f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(detail, dict):
            die(f"cannot read PR {repo}#{number}")
        files = api_get_list(
            config,
            f"/repos/{owner}/{repo}/pulls/{number}/files",
        )
        comments = api_get_list(
            config,
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
        )
        maya = latest_maya(
            comments, str(config.get("api", {}).get("maya_author", "wuwei_kubuds"))
        )
        changed = files
        head = detail.get("head") or {}
        base = detail.get("base") or {}
        result.append(
            {
                "repo": repo,
                "number": number,
                "url": detail.get("html_url")
                or detail.get("web_url")
                or f"https://gitcode.com/{owner}/{repo}/merge_requests/{number}",
                "title": detail.get("title"),
                "base_branch": base.get("ref"),
                "head_branch": head.get("ref"),
                "head_sha": head.get("sha"),
                "head_repo": (head.get("repo") or {}).get("html_url"),
                "base_repo": (base.get("repo") or {}).get("html_url"),
                "changed_files": [f.get("filename") for f in changed if f.get("filename")],
                "changed_specs": sorted(
                    f["filename"]
                    for f in changed
                    if isinstance(f.get("filename"), str) and f["filename"].endswith(".spec")
                ),
                "maya": maya,
            }
        )
    return result


def latest_maya(
    comments: list[dict[str, Any]], maya_author: str = "wuwei_kubuds"
) -> dict[str, Any]:
    selected = None
    def comment_time(value: dict[str, Any]) -> float:
        raw = value.get("created_at")
        if not isinstance(raw, str):
            return 0.0
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    for comment in sorted(comments, key=comment_time):
        body = comment.get("body") or ""
        user = (comment.get("user") or {}).get("login")
        if user == maya_author and "构建验证结果" in body:
            selected = comment
    if not selected:
        return {"verdict": "UNKNOWN", "architectures": {}}
    body = selected.get("body") or ""
    architectures: dict[str, str] = {}
    for arch, value in re.findall(
        r"\|\s*\*\*(x86_64|aarch64|riscv64)\*\*\s*\|\s*([^|]+)\|",
        body,
    ):
        architectures[arch] = value.strip()
    verdict = "FAIL" if any("失败" in value for value in architectures.values()) else "PASS"
    return {
        "verdict": verdict,
        "architectures": architectures,
        "comment": body,
        "created_at": selected.get("created_at"),
    }


def ensure_git_source(
    repo_url: str,
    branch: str,
    expected_head: str | None,
    destination: Path,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        command = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            repo_url,
            str(destination),
        ]
    else:
        remote = subprocess.run(
            ["git", "-C", str(destination), "remote", "get-url", "origin"],
            text=True,
            capture_output=True,
        )
        if remote.returncode:
            die(f"cached source is not a Git repository: {destination}")
        if remote.stdout.strip().rstrip("/").removesuffix(".git") != repo_url.rstrip(
            "/"
        ).removesuffix(".git"):
            die(
                f"cached source origin mismatch for {destination}: "
                f"{remote.stdout.strip()} != {repo_url}"
            )
        command = [
            "git",
            "-C",
            str(destination),
            "fetch",
            "--depth",
            "1",
            "origin",
            f"+{branch}:refs/remotes/origin/{branch}",
        ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        die(
            f"cannot materialize {repo_url} branch {branch}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    checkout = subprocess.run(
        ["git", "-C", str(destination), "checkout", "--detach", f"origin/{branch}"],
        text=True,
        capture_output=True,
    )
    if checkout.returncode:
        die(
            f"cannot checkout {expected_head or branch} from {repo_url}: "
            f"{checkout.stderr.strip() or checkout.stdout.strip()}"
        )
    actual = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    if expected_head and actual != expected_head:
        die(
            f"{repo_url} branch {branch} is at {actual}, expected PR head "
            f"{expected_head}; preserve a local source mapping for a frozen snapshot"
        )
    return destination


def expand_macros(value: str, definitions: dict[str, str]) -> str:
    for _ in range(4):
        before = value
        value = re.sub(
            r"%\{!?([A-Za-z0-9_]+)\}",
            lambda match: definitions.get(match.group(1), match.group(0)),
            value,
        )
        value = re.sub(
            r"%([A-Za-z0-9_]+)",
            lambda match: definitions.get(match.group(1), match.group(0)),
            value,
        )
        if value == before:
            break
    return value


def normalize_package(name: str, distro: str) -> str:
    name = name.strip().strip("\"'")
    name = name.replace("_", "-")
    if not name.startswith("ros-") and not any(c in name for c in "./%"):
        name = f"ros-{distro}-{name}"
    return name


def parse_dependency(
    value: str,
    distro: str,
    *,
    ros_package: bool = False,
) -> dict[str, str | None]:
    value = value.split("#", 1)[0].strip()
    match = CONSTRAINT_RE.match(value)
    if not match:
        name = normalize_package(value, distro) if ros_package else value
        return {"name": name, "operator": None, "version": None}
    name, operator, version = match.groups()
    return {
        "name": normalize_package(name, distro) if ros_package else name,
        "operator": operator,
        "version": version.strip() or None,
    }


def rpmvercmp(left: str, right: str) -> int:
    """Small deterministic approximation for planner conflict detection."""

    def pieces(value: str) -> list[tuple[int, Any]]:
        return [
            (0, int(piece)) if piece.isdigit() else (1, piece)
            for piece in re.findall(r"\d+|[A-Za-z]+", value)
        ]

    a = pieces(left)
    b = pieces(right)
    for x, y in zip(a, b):
        if x == y:
            continue
        if x[0] != y[0]:
            return -1 if x[0] < y[0] else 1
        return -1 if x[1] < y[1] else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def constraint_satisfied(version: str, operator: str | None, required: str | None) -> bool:
    if not operator or not required:
        return True
    cmp = rpmvercmp(version, required)
    return {
        "=": cmp == 0,
        ">": cmp > 0,
        ">=": cmp >= 0,
        "<": cmp < 0,
        "<=": cmp <= 0,
    }[operator]


def package_xml_metadata(
    source: Path, source_name: str
) -> tuple[str | None, list[tuple[str, str | None, str | None]]]:
    if not source_name or not Path(source_name).name:
        return None, []
    candidate = source / Path(source_name).name
    if not candidate.is_file():
        return None, []
    try:
        if tarfile.is_tarfile(candidate):
            with tarfile.open(candidate, "r:*") as archive:
                member = next(
                    (m for m in archive.getmembers() if m.name.endswith("/package.xml")),
                    None,
                )
                if member is None:
                    return None, []
                stream = archive.extractfile(member)
                if stream is None:
                    return None, []
                root = ET.fromstring(stream.read())
        else:
            root = ET.parse(candidate).getroot()
    except (tarfile.TarError, ET.ParseError, OSError, EOFError):
        return None, []
    dependencies: list[tuple[str, str | None, str | None]] = []
    for tag in ("depend", "build_depend", "buildtool_depend"):
        for element in root.findall(tag):
            if not element.text or not element.text.strip():
                continue
            operator = None
            version = None
            for attribute, candidate in (
                ("version_gte", ">="),
                ("version_lte", "<="),
                ("version_eq", "="),
            ):
                if element.get(attribute):
                    operator = candidate
                    version = element.get(attribute)
                    break
            dependencies.append((element.text.strip(), operator, version))
    return (root.findtext("name") or "").strip() or None, sorted(set(dependencies))


def parse_spec(path: Path, distro: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    definitions = {
        match.group(1): match.group(2).strip()
        for match in DEFINE_RE.finditer(text)
    }
    definitions.setdefault("ros_distro", distro)
    expanded = expand_macros(text, definitions)
    name_match = NAME_RE.search(expanded)
    version_match = VERSION_RE.search(expanded)
    if not name_match:
        die(f"spec has no Name: {path}")
    name = name_match.group(1)
    version = version_match.group(1) if version_match else "0"
    definitions.update({"name": name, "version": version})
    expanded = expand_macros(expanded, definitions)
    dependencies = []
    for _, value in DEP_RE.findall(expanded):
        dependencies.extend(parse_dependency(part, distro) for part in value.split(","))
    source_match = SOURCE_RE.search(expanded)
    source_name = source_match.group(1) if source_match else ""
    xml_name, xml_dependencies = package_xml_metadata(path.parent, source_name)
    provides = {name}
    for value in PROVIDES_RE.findall(expanded):
        provides.add(value.split()[0])
    for named, value in PACKAGE_RE.findall(expanded):
        if "%" not in value:
            provides.add(value if named else f"{name}-{value}")
    return {
        "package": name,
        "version": version,
        "provides": sorted(provides),
        "dependencies": dependencies,
        "package_xml_name": xml_name,
        "package_xml_dependencies": [
            {
                "name": normalize_package(name, distro),
                "operator": operator,
                "version": version,
            }
            for name, operator, version in xml_dependencies
        ],
    }


def topo_sort(nodes: dict[str, dict[str, Any]], edges: set[tuple[str, str]]) -> tuple[list[str], list[list[str]]]:
    outgoing: dict[str, set[str]] = defaultdict(set)
    indegree = {key: 0 for key in nodes}
    for source, target in edges:
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = sorted(key for key, value in indegree.items() if value == 0)
    order = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
        ready.sort()
    cycles: list[list[str]] = []
    if len(order) != len(nodes):
        outgoing_nodes: dict[str, list[str]] = {key: [] for key in nodes}
        for source, target in sorted(edges):
            outgoing_nodes[source].append(target)
        index = 0
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for target in outgoing_nodes[node]:
                if target not in indices:
                    visit(target)
                    lowlinks[node] = min(lowlinks[node], lowlinks[target])
                elif target in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[target])
            if lowlinks[node] != indices[node]:
                return
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            component.sort()
            self_loop = len(component) == 1 and (
                component[0],
                component[0],
            ) in edges
            if len(component) > 1 or self_loop:
                cycles.append(component)

        for node in sorted(nodes):
            if node not in indices:
                visit(node)
        cycles.sort(key=lambda values: values[0])
    return order, cycles


def discover_and_plan(
    config: dict[str, Any], source_root: Path | None = None
) -> dict[str, Any]:
    prs = discover_prs(config)
    if not prs:
        die("no PRs matched the group")
    sources = config.get("sources", {})
    default_sources = config.get("default_sources", {})
    if not isinstance(sources, dict) or not isinstance(default_sources, dict):
        die("sources and default_sources must be objects")
    distro = str(config.get("target_distro", "humble"))
    nodes: dict[str, dict[str, Any]] = {}
    changed_sources: dict[tuple[str, str], set[Path]] = defaultdict(set)
    changed_prs: dict[tuple[str, str], set[str]] = defaultdict(set)
    pinned_dependencies = config.get("pinned_dependencies", [])
    if not isinstance(pinned_dependencies, list):
        die("pinned_dependencies must be an array")

    for pr in prs:
        key = f"{pr['repo']}#{pr['number']}"
        source_value = sources.get(key) or default_sources.get(pr["repo"])
        if source_value:
            source = Path(source_value).expanduser().resolve()
        else:
            repo_url = pr.get("head_repo") or pr.get("base_repo")
            branch = pr.get("head_branch")
            head_sha = pr.get("head_sha")
            if not all(isinstance(value, str) and value for value in (repo_url, branch, head_sha)):
                die(f"PR {key} does not expose enough Git metadata for automatic checkout")
            root = source_root or Path("/tmp/oe-ros-joint-upgrade/sources")
            source = ensure_git_source(
                repo_url,
                branch,
                head_sha,
                root / f"{pr['repo']}-{pr['number']}",
            ).resolve()
        if not source.is_dir():
            die(f"source is not a directory for {key}: {source}")
        pr["source"] = str(source)
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
        ).stdout.strip()
        pr["local_head"] = head
        if head != pr["head_sha"] and config.get("strict_head", True):
            die(
                f"{key} local HEAD {head} does not match PR head {pr['head_sha']}; "
                "fetch/checkout the PR head or set strict_head=false explicitly"
            )
        multibuild = source / "_multibuild"
        multibuild_changed = "_multibuild" in pr.get("changed_files", [])
        if multibuild.is_file() and multibuild_changed:
            xml = ET.parse(multibuild)
            impact_specs = {
                f"{flavor.text.strip()}.spec"
                for flavor in xml.findall(".//flavor")
                if flavor.text and (source / f"{flavor.text.strip()}.spec").is_file()
            }
            impact_specs.update(
                spec
                for spec in pr["changed_specs"]
                if (source / spec).is_file()
            )
            impact_specs = sorted(impact_specs)
        else:
            impact_specs = sorted(
                spec
                for spec in set(pr["changed_specs"])
                if (source / spec).is_file()
            )
        pr["multibuild_expanded"] = bool(
            multibuild.is_file() and multibuild_changed
        )
        pr["impact_specs"] = impact_specs
        for spec in pr["changed_specs"]:
            changed_sources[(pr["repo"], spec)].add(source)
            changed_prs[(pr["repo"], spec)].add(f"{pr['repo']}#{pr['number']}")

    source_conflicts = [
        {
            "repo": repo,
            "spec": spec,
            "sources": [str(value) for value in sorted(sources_for_spec)],
        }
        for (repo, spec), sources_for_spec in sorted(changed_sources.items())
        if len(sources_for_spec) > 1
    ]
    changed_file_owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    repo_branches: dict[str, set[str]] = defaultdict(set)
    for pr in prs:
        branch = pr.get("base_branch")
        if isinstance(branch, str) and branch:
            repo_branches[pr["repo"]].add(branch)
        for filename in pr.get("changed_files", []):
            if isinstance(filename, str) and filename:
                changed_file_owners[(pr["repo"], filename)].add(
                    f"{pr['repo']}#{pr['number']}"
                )
    target_branch_conflicts = [
        {"repo": repo, "branches": sorted(branches)}
        for repo, branches in sorted(repo_branches.items())
        if len(branches) > 1
    ]
    changed_file_conflicts = [
        {"repo": repo, "file": filename, "prs": sorted(owners)}
        for (repo, filename), owners in sorted(changed_file_owners.items())
        if len(owners) > 1
    ]
    changed_source = {
        key: next(iter(values))
        for key, values in changed_sources.items()
        if len(values) == 1
    }

    for pr in prs:
        default_source = Path(pr["source"]).expanduser().resolve()
        for spec in pr["impact_specs"]:
            node_key = f"{pr['repo']}:{spec}"
            source = changed_source.get((pr["repo"], spec), default_source)
            if node_key not in nodes:
                metadata = parse_spec(source / spec, distro)
                nodes[node_key] = {
                    "key": node_key,
                    "repo": pr["repo"],
                    "spec": spec,
                    "source": str(source),
                    "package": metadata["package"],
                    "version": metadata["version"],
                    "provides": metadata["provides"],
                    "dependencies": metadata["dependencies"],
                    "package_xml_name": metadata["package_xml_name"],
                    "package_xml_dependencies": metadata["package_xml_dependencies"],
                    "prs": [],
                }
            if f"{pr['repo']}#{pr['number']}" not in nodes[node_key]["prs"]:
                nodes[node_key]["prs"].append(f"{pr['repo']}#{pr['number']}")

    for dependency in pinned_dependencies:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str):
            die(f"invalid pinned dependency: {dependency!r}")
        local_path = dependency.get("local_path")
        if local_path:
            source = Path(str(local_path)).expanduser().resolve()
        else:
            repo_url = dependency.get("repo")
            branch = dependency.get("branch")
            if not isinstance(repo_url, str) or not isinstance(branch, str):
                die(f"pinned dependency {dependency['name']} requires local_path or repo+branch")
            root = source_root or Path("/tmp/oe-ros-joint-upgrade/sources")
            source = ensure_git_source(
                repo_url,
                branch,
                dependency.get("head_sha"),
                root / f"pinned-{dependency['name']}",
            ).resolve()
        if not source.is_dir():
            die(f"pinned dependency source is not a directory: {source}")
        specs = dependency.get("specs")
        if not isinstance(specs, list) or not specs or not all(
            isinstance(value, str) for value in specs
        ):
            die(f"pinned dependency {dependency['name']} requires a specs array")
        for spec in specs:
            node_key = f"pinned:{dependency['name']}:{spec}"
            metadata = parse_spec(source / spec, distro)
            nodes[node_key] = {
                "key": node_key,
                "repo": f"pinned-{dependency['name']}",
                "spec": spec,
                "source": str(source),
                "package": metadata["package"],
                "version": metadata["version"],
                "provides": metadata["provides"],
                "dependencies": metadata["dependencies"],
                "package_xml_name": metadata["package_xml_name"],
                "package_xml_dependencies": metadata["package_xml_dependencies"],
                "prs": [],
                "pinned": True,
                "prerequisite_for_all": bool(dependency.get("prerequisite_for_all")),
                "rpmbuild_args": dependency.get("rpmbuild_args"),
                "test_policy": dependency.get("test_policy"),
            }

    providers: dict[str, list[str]] = defaultdict(list)
    for key, node in nodes.items():
        for provided in node.get("provides", [node["package"]]):
            providers[provided].append(key)
        if node["package_xml_name"]:
            providers[normalize_package(node["package_xml_name"], distro)].append(key)
    version_conflicts = []
    for package, keys in providers.items():
        versions = {nodes[key]["version"] for key in keys}
        if len(set(keys)) > 1 and len(versions) > 1:
            version_conflicts.append(
                {"package": package, "nodes": sorted(set(keys)), "versions": sorted(versions)}
            )

    edges: set[tuple[str, str]] = set()
    requirement_conflicts = []
    prerequisites = {
        key for key, node in nodes.items() if node.get("prerequisite_for_all")
    }
    for prerequisite in prerequisites:
        for consumer in nodes:
            if consumer != prerequisite and consumer not in prerequisites:
                edges.add((prerequisite, consumer))
    for key, node in nodes.items():
        dependencies = node["dependencies"] + node["package_xml_dependencies"]
        for dependency in dependencies:
            package = dependency["name"]
            for provider in providers.get(package, []):
                if provider == key:
                    continue
                edges.add((provider, key))
                if not constraint_satisfied(
                    nodes[provider]["version"],
                    dependency.get("operator"),
                    dependency.get("version"),
                ):
                    requirement_conflicts.append(
                        {
                            "consumer": key,
                            "provider": provider,
                            "requirement": dependency,
                            "provided_version": nodes[provider]["version"],
                        }
                    )
    all_pr_keys = {f"{pr['repo']}#{pr['number']}" for pr in prs}
    failed_pr_keys = {
        f"{pr['repo']}#{pr['number']}"
        for pr in prs
        if (pr.get("maya") or {}).get("verdict") == "FAIL"
    }
    selection_mode = "explicit-selectors" if config.get("selectors") is not None else "discovery"
    roots = (
        failed_pr_keys
        if config.get("maya_failed_only") and selection_mode == "discovery"
        else all_pr_keys
    )
    if not roots:
        die("no failing PRs matched the discovery window")
    selected_nodes = {
        key for key, node in nodes.items() if roots.intersection(node.get("prs", []))
    }
    incoming: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        incoming[target].add(source)
    pending = list(selected_nodes)
    while pending:
        consumer = pending.pop()
        for provider in incoming.get(consumer, set()):
            if provider not in selected_nodes:
                selected_nodes.add(provider)
                pending.append(provider)
    if not selected_nodes:
        die("selected PRs contain no resolvable spec impact")
    selected_prs = {
        pr_key
        for key in selected_nodes
        for pr_key in nodes[key].get("prs", [])
    }
    prs = [
        pr for pr in prs if f"{pr['repo']}#{pr['number']}" in selected_prs
    ]
    nodes = {key: value for key, value in nodes.items() if key in selected_nodes}
    edges = {
        (source, target)
        for source, target in edges
        if source in selected_nodes and target in selected_nodes
    }
    version_conflicts = [
        conflict
        for conflict in version_conflicts
        if set(conflict["nodes"]).issubset(selected_nodes)
    ]
    requirement_conflicts = [
        conflict
        for conflict in requirement_conflicts
        if conflict["consumer"] in selected_nodes
        and conflict["provider"] in selected_nodes
    ]
    source_conflicts = [
        conflict
        for conflict in source_conflicts
        if f"{conflict['repo']}:{conflict['spec']}" in selected_nodes
    ]
    selected_repos = {node["repo"] for node in nodes.values()}
    target_branch_conflicts = [
        conflict
        for conflict in target_branch_conflicts
        if conflict["repo"] in selected_repos
    ]
    changed_file_conflicts = [
        conflict
        for conflict in changed_file_conflicts
        if conflict["repo"] in selected_repos
        and set(conflict["prs"]).issubset(selected_prs)
    ]
    order, cycles = topo_sort(nodes, edges)
    pr_relations: dict[str, dict[str, set[str]]] = {
        pr_key: {"producers": set(), "consumers": set()} for pr_key in selected_prs
    }
    for provider, consumer in edges:
        provider_node = nodes[provider]
        consumer_node = nodes[consumer]
        provider_prs = changed_prs.get(
            (provider_node["repo"], provider_node["spec"]), set()
        )
        consumer_prs = changed_prs.get(
            (consumer_node["repo"], consumer_node["spec"]), set()
        )
        for provider_pr in provider_prs:
            for consumer_pr in consumer_prs:
                if provider_pr == consumer_pr:
                    continue
                pr_relations[provider_pr]["consumers"].add(consumer_pr)
                pr_relations[consumer_pr]["producers"].add(provider_pr)
    return {
        "schema_version": 1,
        "group_id": config.get("group_id"),
        "discovered_at": datetime.now().astimezone().isoformat(),
        "prs": prs,
        "pinned_dependencies": pinned_dependencies,
        "selection": {
            "mode": selection_mode,
            "root_prs": sorted(roots),
            "selected_prs": sorted(selected_prs),
            "included_dependency_prs": sorted(selected_prs - roots),
        },
        "producer_prs": sorted(
            pr_key for pr_key, value in pr_relations.items() if value["consumers"]
        ),
        "consumer_prs": sorted(
            pr_key for pr_key, value in pr_relations.items() if value["producers"]
        ),
        "pr_relations": {
            pr_key: {
                "producers": sorted(value["producers"]),
                "consumers": sorted(value["consumers"]),
            }
            for pr_key, value in sorted(pr_relations.items())
        },
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": [{"from": source, "to": target} for source, target in sorted(edges)],
        "topological_order": order,
        "merge_order": order,
        "cycles": cycles,
        "source_conflicts": source_conflicts,
        "target_branch_conflicts": target_branch_conflicts,
        "changed_file_conflicts": changed_file_conflicts,
        "version_conflicts": version_conflicts,
        "requirement_conflicts": requirement_conflicts,
        "runnable": not cycles
        and not source_conflicts
        and not target_branch_conflicts
        and not changed_file_conflicts
        and not version_conflicts
        and not requirement_conflicts,
    }


def batch_manifest(config: dict[str, Any], plan: dict[str, Any], arch: str) -> dict[str, Any]:
    if not plan["runnable"]:
        die(
            "group plan contains cycles, source/branch conflicts, or version conflicts"
        )
    policy_overrides = config.get("spec_test_policies", {})
    if not isinstance(policy_overrides, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and value in {"required", "allow_empty", "skip", "report"}
        for key, value in policy_overrides.items()
    ):
        die(
            "spec_test_policies must map node keys to "
            "required, allow_empty, skip, or report"
        )
    node_by_key = {node["key"]: node for node in plan["nodes"]}
    grouped: list[dict[str, Any]] = []
    for key in plan["topological_order"]:
        node = node_by_key[key]
        test_policy = (
            node.get("test_policy")
            or policy_overrides.get(key)
            or config.get("test_policy", "report")
        )
        if (
            grouped
            and grouped[-1]["local_path"] == node["source"]
            and grouped[-1]["_package_repo"] == node["repo"]
            and grouped[-1]["rpmbuild_args"]
            == (node.get("rpmbuild_args") or config.get("rpmbuild_args", ["--with", "tests"]))
            and grouped[-1]["test_policy"] == test_policy
        ):
            grouped[-1]["specs"].append(node["spec"])
            grouped[-1]["_nodes"].append(node["key"])
        else:
            grouped.append(
                {
                    "name": f"{node['repo']}-{len(grouped) + 1:03d}",
                    "local_path": node["source"],
                    "_package_repo": node["repo"],
                    "rpmbuild_args": node.get("rpmbuild_args")
                    or config.get("rpmbuild_args", ["--with", "tests"]),
                    "test_policy": test_policy,
                    "specs": [node["spec"]],
                    "_nodes": [node["key"]],
                }
            )
    targets = []
    for item in grouped:
        targets.append(
            {
                "name": item["name"],
                "local_path": item["local_path"],
                "rpmbuild_args": item["rpmbuild_args"],
                "test_policy": item["test_policy"],
                "expose_buildroot_in_tests": config.get(
                    "expose_buildroot_in_tests", True
                ),
                "specs": item["specs"],
                "group_nodes": item["_nodes"],
            }
        )
    image = (config.get("images") or {}).get(arch)
    if not image:
        die(f"no image configured for {arch}")
    group_id = plan["group_id"]
    output = str(Path(config.get("output_root", f"/tmp/{group_id}")) / arch)
    return {
        "group_id": group_id,
        "architecture": arch,
        "platform": "linux/arm64" if arch == "aarch64" else "linux/amd64",
        "image": image,
        "dist": config.get("dist", ".oe2403"),
        "target_distro": config.get("target_distro", "humble"),
        "output_dir": output,
        "repo_files": config.get("repo_files", []),
        "fixtures": config.get("fixtures", []),
        "setup_scripts": config.get("setup_scripts", []),
        "continue_on_error": True,
        "dependencies": [],
        "targets": targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arch", choices=["x86_64", "aarch64"])
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--combine", nargs="+", type=Path)
    args = parser.parse_args()
    if args.combine:
        summaries = [read_json(path) for path in args.combine]
        group_ids = {summary.get("group_id") for summary in summaries}
        if len(group_ids) != 1:
            die(f"cannot combine different groups: {sorted(group_ids)}")
        selections = {
            json.dumps(summary.get("selection") or {}, sort_keys=True)
            for summary in summaries
        }
        if len(selections) != 1:
            die("cannot combine summaries with different PR selections")
        architectures = {
            summary["architecture"]: summary.get("status", "failed")
            for summary in summaries
        }
        pr_names = sorted(
            {
                name
                for summary in summaries
                for name in (summary.get("prs") or {})
            }
        )
        pr_status = {
            name: (
                "passed"
                if all(
                    (summary.get("prs") or {}).get(name) == "passed"
                    for summary in summaries
                )
                else "failed"
            )
            for name in pr_names
        }
        spec_names = sorted(
            {
                name
                for summary in summaries
                for name in (summary.get("specs") or {})
            }
        )
        spec_status = {
            name: (
                "passed"
                if all(
                    (summary.get("specs") or {}).get(name) == "passed"
                    for summary in summaries
                )
                else "failed"
            )
            for name in spec_names
        }
        passed = bool(summaries) and all(
            value == "passed" for value in architectures.values()
        ) and all(value == "passed" for value in pr_status.values()) and all(
            value == "passed" for value in spec_status.values()
        )
        combined = {
            "group_id": group_ids.pop(),
            "status": "passed" if passed else "failed",
            "allowed_to_enter_merge_train": passed,
            "architectures": architectures,
            "prs": pr_status,
            "selection": summaries[0].get("selection", {}) if summaries else {},
            "specs": spec_status,
            "sources": [str(path) for path in args.combine],
        }
        write_json(args.output / "group-summary.json", combined)
        print(f"combined group status: {combined['status']}")
        if not passed:
            raise SystemExit(1)
        return
    if args.config is None:
        die("--config is required unless --combine is used")
    config = read_json(args.config)
    plan = discover_and_plan(config, args.output / "sources")
    write_json(args.output / "group-plan.json", plan)
    print(
        f"group={plan['group_id']} prs={len(plan['prs'])} "
        f"specs={len(plan['nodes'])} runnable={plan['runnable']}"
    )
    if not plan["runnable"]:
        raise SystemExit(1)
    if args.arch:
        manifest = batch_manifest(config, plan, args.arch)
        manifest_path = args.output / f"batch-{args.arch}.json"
        write_json(manifest_path, manifest)
        print(f"manifest={manifest_path}")
        if args.run:
            builder = Path(__file__).with_name("batch_build.py")
            status = subprocess.run(
                [sys.executable, str(builder), "--manifest", str(manifest_path)]
            ).returncode
            latest = Path(manifest["output_dir"]) / "latest-summary.json"
            atomic: dict[str, Any] = {
                "group_id": plan["group_id"],
                "architecture": args.arch,
                "status": "failed",
                "allowed_to_enter_merge_train": False,
                "selection": plan.get("selection", {}),
                "prs": {},
                "specs": {},
                "builder_summary": str(latest),
            }
            if latest.is_file():
                build_summary = read_json(latest)
                item_status = {
                    item["name"]: item.get("status", "failed")
                    for item in build_summary.get("items", [])
                }
                node_status: dict[str, bool] = {}
                for node in plan["nodes"]:
                    passed = False
                    for item in manifest["targets"]:
                        if node["key"] in item["group_nodes"]:
                            passed = item_status.get(item["name"]) == "passed"
                    node_status[node["key"]] = passed
                pr_status = {}
                for pr in plan["prs"]:
                    pr_key = f"{pr['repo']}#{pr['number']}"
                    pr_nodes = [
                        node["key"] for node in plan["nodes"] if pr_key in node["prs"]
                    ]
                    pr_status[pr_key] = (
                        "passed"
                        if pr_nodes and all(node_status.get(key, False) for key in pr_nodes)
                        else "failed"
                    )
                passed = status == 0 and bool(pr_status) and all(
                    value == "passed" for value in pr_status.values()
                )
                atomic.update(
                    {
                        "status": "passed" if passed else "failed",
                        "allowed_to_enter_merge_train": passed,
                        "prs": pr_status,
                        "specs": {
                            key: "passed" if value else "failed"
                            for key, value in sorted(node_status.items())
                        },
                    }
                )
            write_json(args.output / f"group-summary-{args.arch}.json", atomic)
            if status:
                raise SystemExit(status)


if __name__ == "__main__":
    main()
