#!/usr/bin/env python3
"""Assemble one atomic architecture result from ordered batch attempts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"error: {path} must contain a JSON object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--architecture", required=True, choices=["x86_64", "aarch64"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--summary",
        required=True,
        action="append",
        type=Path,
        help="Attempt summary. Repeat in chronological order; later attempts override "
        "overlapping group nodes.",
    )
    args = parser.parse_args()

    plan = read_json(args.plan)
    nodes = {
        node["key"]: node
        for node in plan.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("key"), str)
    }
    if not nodes:
        raise SystemExit("error: plan contains no nodes")
    node_status = {key: False for key in nodes}
    attempts: list[dict[str, Any]] = []

    for summary_path in args.summary:
        summary = read_json(summary_path)
        if summary.get("group_id") != plan.get("group_id"):
            raise SystemExit(
                f"error: {summary_path} group_id does not match {plan.get('group_id')}"
            )
        summary_arch = summary.get("architecture")
        if summary_arch not in {None, args.architecture}:
            raise SystemExit(
                f"error: {summary_path} architecture {summary_arch} does not match "
                f"{args.architecture}"
            )
        manifest_path = summary_path.resolve().parent / "manifest.json"
        if not manifest_path.is_file():
            value = summary.get("manifest")
            if not isinstance(value, str):
                raise SystemExit(f"error: cannot locate manifest for {summary_path}")
            manifest_path = Path(value)
        manifest = read_json(manifest_path)
        if manifest.get("group_id") != plan.get("group_id"):
            raise SystemExit(
                f"error: {manifest_path} group_id does not match {plan.get('group_id')}"
            )
        item_status = {
            item.get("name"): item.get("status", "failed")
            for item in summary.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        items_by_name = {
            item.get("name"): item
            for item in summary.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        covered: list[str] = []
        for target in manifest.get("targets", []):
            if not isinstance(target, dict):
                continue
            target_nodes = target.get("group_nodes") or [
                key
                for key, node in nodes.items()
                if node.get("source") == target.get("local_path")
                and node.get("spec") in (target.get("specs") or [])
            ]
            item = items_by_name.get(target.get("name"))
            item_passed = item_status.get(target.get("name")) == "passed"
            test_policy = target.get("test_policy", "required")
            global_stage_failed = bool(
                item
                and (
                    (item.get("install") or {}).get("status") == "failed"
                    or (item.get("duplicate_check") or {}).get("status")
                    == "failed"
                )
            )
            spec_results = {
                result.get("spec"): result
                for result in (item or {}).get("specs", [])
                if isinstance(result, dict) and isinstance(result.get("spec"), str)
            }
            for key in target_nodes:
                if key in node_status:
                    spec = nodes[key].get("spec")
                    spec_result = spec_results.get(spec)
                    spec_passed = bool(
                        item_passed
                        or (
                            item
                            and not global_stage_failed
                            and spec_result is not None
                            and spec_result.get("status") != "failed"
                            and (
                                test_policy == "report"
                                or (spec_result.get("tests") or {}).get("status")
                                != "failed"
                            )
                        )
                    )
                    node_status[key] = spec_passed
                    covered.append(key)
        attempts.append(
            {
                "summary": str(summary_path.resolve()),
                "attempt_id": summary.get("attempt_id"),
                "covered_nodes": sorted(set(covered)),
            }
        )

    pr_status: dict[str, str] = {}
    for pr in plan.get("prs", []):
        if not isinstance(pr, dict):
            continue
        pr_key = f"{pr['repo']}#{pr['number']}"
        pr_nodes = [
            key for key, node in nodes.items() if pr_key in node.get("prs", [])
        ]
        pr_status[pr_key] = (
            "passed"
            if pr_nodes and all(node_status[key] for key in pr_nodes)
            else "failed"
        )

    all_passed = all(node_status.values()) and bool(pr_status) and all(
        value == "passed" for value in pr_status.values()
    )
    result = {
        "group_id": plan.get("group_id"),
        "architecture": args.architecture,
        "status": "passed" if all_passed else "failed",
        "allowed_to_enter_merge_train": all_passed,
        "selection": plan.get("selection", {}),
        "prs": pr_status,
        "specs": {
            key: "passed" if passed else "failed"
            for key, passed in sorted(node_status.items())
        },
        "attempts": attempts,
    }
    write_json(args.output, result)
    print(f"assembled {args.architecture} group status: {result['status']}")
    if not all_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
