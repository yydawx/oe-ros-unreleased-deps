#!/usr/bin/env python3
"""Fast invariant tests for the joint-upgrade planner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("joint_upgrade.py")
SPEC = importlib.util.spec_from_file_location("joint_upgrade", SCRIPT)
assert SPEC and SPEC.loader
joint_upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(joint_upgrade)

BATCH_SCRIPT = Path(__file__).with_name("batch_build.py")
BATCH_SPEC = importlib.util.spec_from_file_location("batch_build", BATCH_SCRIPT)
assert BATCH_SPEC and BATCH_SPEC.loader
batch_build = importlib.util.module_from_spec(BATCH_SPEC)
BATCH_SPEC.loader.exec_module(batch_build)

ASSEMBLE_SCRIPT = Path(__file__).with_name("assemble_group_results.py")
ASSEMBLE_SPEC = importlib.util.spec_from_file_location(
    "assemble_group_results", ASSEMBLE_SCRIPT
)
assert ASSEMBLE_SPEC and ASSEMBLE_SPEC.loader
assemble_group_results = importlib.util.module_from_spec(ASSEMBLE_SPEC)
ASSEMBLE_SPEC.loader.exec_module(assemble_group_results)


class PlannerTests(unittest.TestCase):
    def test_topological_order_places_producers_first(self) -> None:
        nodes = {name: {} for name in ("consumer", "producer", "shared")}
        edges = {("producer", "consumer"), ("producer", "shared")}
        order, cycles = joint_upgrade.topo_sort(nodes, edges)
        self.assertEqual(cycles, [])
        self.assertLess(order.index("producer"), order.index("consumer"))
        self.assertLess(order.index("producer"), order.index("shared"))

    def test_cycles_are_reported_as_strongly_connected_components(self) -> None:
        nodes = {name: {} for name in ("a", "b", "c", "d")}
        edges = {("a", "b"), ("b", "c"), ("c", "a"), ("d", "d")}
        _, cycles = joint_upgrade.topo_sort(nodes, edges)
        self.assertEqual(cycles, [["a", "b", "c"], ["d"]])

    def test_latest_maya_comment_wins(self) -> None:
        comments = [
            {
                "user": {"login": "wuwei_kubuds"},
                "created_at": "2026-11-01T00:00:00+08:00",
                "body": "## 构建验证结果\n| **x86_64** | ✅ 全部成功 |",
            },
            {
                "user": {"login": "wuwei_kubuds"},
                "created_at": "2026-11-02T00:00:00+08:00",
                "body": "## 构建验证结果\n| **x86_64** | ⚠️ 部分失败 |",
            },
        ]
        self.assertEqual(joint_upgrade.latest_maya(comments)["verdict"], "FAIL")

    def test_spec_macros_and_package_xml_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.xml").write_text(
                "<package><name>demo_pkg</name>"
                '<depend version_gte="3.0.0">rclcpp</depend></package>',
                encoding="utf-8",
            )
            (root / "demo.spec").write_text(
                "%define ros_distro humble\n"
                "Name: ros-%{ros_distro}-demo\n"
                "Version: 1.2.3\n"
                "BuildRequires: ros-%{ros_distro}-rclcpp\n"
                "Source0: package.xml\n",
                encoding="utf-8",
            )
            parsed = joint_upgrade.parse_spec(root / "demo.spec", "humble")
        self.assertEqual(parsed["package"], "ros-humble-demo")
        self.assertEqual(parsed["package_xml_name"], "demo_pkg")
        dependencies = [
            dependency["name"]
            for dependency in (
                parsed["dependencies"] + parsed["package_xml_dependencies"]
            )
        ]
        self.assertIn("ros-humble-rclcpp", dependencies)
        self.assertEqual(
            parsed["package_xml_dependencies"][0]["operator"], ">="
        )
        self.assertEqual(
            parsed["package_xml_dependencies"][0]["version"], "3.0.0"
        )

    def test_manifest_keeps_every_spec_and_exposes_buildroot(self) -> None:
        config = {
            "group_id": "group-42",
            "target_distro": "humble",
            "rpmbuild_args": ["--with", "tests"],
            "test_policy": "required",
            "expose_buildroot_in_tests": True,
            "images": {"x86_64": "builder"},
            "output_root": "/tmp/group-42",
        }
        nodes = []
        order = []
        for spec in ("a.spec", "b.spec"):
            key = f"repo:{spec}"
            order.append(key)
            nodes.append(
                {
                    "key": key,
                    "repo": "repo",
                    "spec": spec,
                    "source": "/tmp/source",
                    "package": spec.removesuffix(".spec"),
                    "version": "1",
                    "provides": [],
                    "dependencies": [],
                    "package_xml_name": None,
                    "package_xml_dependencies": [],
                    "prs": ["repo#1"],
                }
            )
        plan = {
            "group_id": "group-42",
            "runnable": True,
            "nodes": nodes,
            "topological_order": order,
        }
        manifest = joint_upgrade.batch_manifest(config, plan, "x86_64")
        self.assertEqual(manifest["targets"][0]["specs"], ["a.spec", "b.spec"])
        self.assertTrue(manifest["targets"][0]["expose_buildroot_in_tests"])
        self.assertEqual(manifest["target_distro"], "humble")
        self.assertEqual(
            manifest["targets"][0]["group_nodes"],
            ["repo:a.spec", "repo:b.spec"],
        )

    def test_failed_consumer_discovers_passing_producer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / "consumer"
            producer = root / "producer"
            consumer.mkdir()
            producer.mkdir()
            (consumer / "consumer.spec").write_text(
                "Name: ros-humble-consumer\n"
                "Version: 2\n"
                "BuildRequires: ros-humble-producer\n",
                encoding="utf-8",
            )
            (producer / "producer.spec").write_text(
                "Name: ros-humble-producer\n"
                "Version: 3\n",
                encoding="utf-8",
            )
            prs = [
                {
                    "repo": "consumer",
                    "number": 1,
                    "title": "consumer",
                    "base_branch": "humble",
                    "head_branch": "pr-1",
                    "head_sha": "",
                    "changed_files": ["consumer.spec"],
                    "changed_specs": ["consumer.spec"],
                    "maya": {"verdict": "FAIL"},
                },
                {
                    "repo": "producer",
                    "number": 2,
                    "title": "producer",
                    "base_branch": "humble",
                    "head_branch": "pr-2",
                    "head_sha": "",
                    "changed_files": ["producer.spec"],
                    "changed_specs": ["producer.spec"],
                    "maya": {"verdict": "PASS"},
                },
            ]
            config = {
                "group_id": "group-42",
                "target_distro": "humble",
                "default_sources": {
                    "consumer": str(consumer),
                    "producer": str(producer),
                },
                "strict_head": False,
                "maya_failed_only": True,
            }
            with mock.patch.object(joint_upgrade, "discover_prs", return_value=prs):
                plan = joint_upgrade.discover_and_plan(config, root / "sources")
        self.assertEqual(plan["selection"]["root_prs"], ["consumer#1"])
        self.assertEqual(
            plan["selection"]["included_dependency_prs"], ["producer#2"]
        )
        self.assertEqual(
            [edge for edge in plan["edges"] if edge["to"] == "consumer:consumer.spec"],
            [{"from": "producer:producer.spec", "to": "consumer:consumer.spec"}],
        )
        self.assertEqual(plan["producer_prs"], ["producer#2"])
        self.assertEqual(plan["consumer_prs"], ["consumer#1"])

    def test_unchanged_multibuild_does_not_expand_to_every_flavor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "repo"
            source.mkdir()
            (source / "_multibuild").write_text(
                "<multibuild><flavor>a</flavor><flavor>b</flavor></multibuild>",
                encoding="utf-8",
            )
            for spec in ("a.spec", "b.spec"):
                (source / spec).write_text(
                    f"Name: {spec.removesuffix('.spec')}\nVersion: 1\n",
                    encoding="utf-8",
                )
            prs = [
                {
                    "repo": "repo",
                    "number": 1,
                    "title": "a",
                    "base_branch": "master",
                    "head_branch": "pr-1",
                    "head_sha": "",
                    "changed_files": ["a.spec"],
                    "changed_specs": ["a.spec"],
                    "maya": {"verdict": "FAIL"},
                }
            ]
            config = {
                "group_id": "group-42",
                "target_distro": "humble",
                "default_sources": {"repo": str(source)},
                "strict_head": False,
                "maya_failed_only": False,
            }
            with mock.patch.object(joint_upgrade, "discover_prs", return_value=prs):
                plan = joint_upgrade.discover_and_plan(config, root / "sources")
        self.assertEqual(plan["prs"][0]["impact_specs"], ["a.spec"])
        self.assertFalse(plan["prs"][0]["multibuild_expanded"])

    def test_changed_multibuild_expands_all_flavors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "repo"
            source.mkdir()
            (source / "_multibuild").write_text(
                "<multibuild><flavor>a</flavor><flavor>b</flavor></multibuild>",
                encoding="utf-8",
            )
            for spec in ("a.spec", "b.spec"):
                (source / spec).write_text(
                    f"Name: {spec.removesuffix('.spec')}\nVersion: 1\n",
                    encoding="utf-8",
                )
            prs = [
                {
                    "repo": "repo",
                    "number": 1,
                    "title": "multibuild",
                    "base_branch": "master",
                    "head_branch": "pr-1",
                    "head_sha": "",
                    "changed_files": ["_multibuild"],
                    "changed_specs": [],
                    "maya": {"verdict": "FAIL"},
                }
            ]
            config = {
                "group_id": "group-42",
                "target_distro": "humble",
                "default_sources": {"repo": str(source)},
                "strict_head": False,
                "maya_failed_only": False,
            }
            with mock.patch.object(joint_upgrade, "discover_prs", return_value=prs):
                plan = joint_upgrade.discover_and_plan(config, root / "sources")
        self.assertEqual(
            plan["prs"][0]["impact_specs"], ["a.spec", "b.spec"]
        )
        self.assertTrue(plan["prs"][0]["multibuild_expanded"])

    def test_allow_empty_does_not_turn_no_tests_into_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "test.log"
            log.write_text(
                "collected 0 items\n"
                "no tests ran in 0.01s\n"
                "+ echo 'RPM TESTS FAILED'\n",
                encoding="utf-8",
            )
            result = batch_build.assess_test_log(log, "allow_empty")
        self.assertEqual(result["status"], "empty_allowed")

    def test_allow_empty_still_rejects_real_test_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "test.log"
            log.write_text(
                "[  FAILED  ] Example.test\n"
                "The following tests FAILED:\n"
                "RPM TESTS FAILED\n",
                encoding="utf-8",
            )
            result = batch_build.assess_test_log(log, "allow_empty")
        self.assertEqual(result["status"], "failed")

    def test_report_policy_records_without_enforcing_test_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "test.log"
            log.write_text(
                "[  FAILED  ] Example.test\n"
                "The following tests FAILED:\n"
                "RPM TESTS FAILED\n",
                encoding="utf-8",
            )
            result = batch_build.assess_test_log(log, "report")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["enforced"])

    def test_report_policy_is_the_planner_default(self) -> None:
        config = {
            "group_id": "group-42",
            "target_distro": "humble",
            "images": {"x86_64": "builder"},
            "output_root": "/tmp/group-42",
        }
        node = {
            "key": "repo:a.spec",
            "repo": "repo",
            "spec": "a.spec",
            "source": "/tmp/source",
            "package": "a",
            "version": "1",
            "provides": [],
            "dependencies": [],
            "package_xml_name": None,
            "package_xml_dependencies": [],
            "prs": ["repo#1"],
        }
        plan = {
            "group_id": "group-42",
            "runnable": True,
            "nodes": [node],
            "topological_order": ["repo:a.spec"],
        }
        manifest = joint_upgrade.batch_manifest(config, plan, "x86_64")
        self.assertEqual(manifest["targets"][0]["test_policy"], "report")

    def test_architecture_combine_requires_every_pr_and_spec_to_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = {
                "group_id": "group-42",
                "status": "passed",
                "prs": {"repo#1": "passed"},
                "specs": {"repo:a.spec": "passed"},
            }
            x86 = root / "x86.json"
            arm = root / "arm.json"
            x86.write_text(
                json.dumps({**common, "architecture": "x86_64"}), encoding="utf-8"
            )
            arm.write_text(
                json.dumps({**common, "architecture": "aarch64"}), encoding="utf-8"
            )
            output = root / "out"
            with mock.patch.object(
                sys,
                "argv",
                [
                    "joint_upgrade.py",
                    "--combine",
                    str(x86),
                    str(arm),
                    "--output",
                    str(output),
                ],
            ):
                joint_upgrade.main()
            combined = json.loads((output / "group-summary.json").read_text())
        self.assertTrue(combined["allowed_to_enter_merge_train"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "x86.json"
            good.write_text(
                json.dumps({**common, "architecture": "x86_64"}), encoding="utf-8"
            )
            bad = {
                **common,
                "architecture": "aarch64",
                "specs": {"repo:a.spec": "failed"},
            }
            arg = root / "arm.json"
            arg.write_text(json.dumps(bad), encoding="utf-8")
            with mock.patch.object(
                sys,
                "argv",
                [
                    "joint_upgrade.py",
                    "--combine",
                    str(good),
                    str(arg),
                    "--output",
                    str(root / "out"),
                ],
            ), self.assertRaises(SystemExit):
                joint_upgrade.main()

    def test_pr_source_can_be_materialized_from_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            origin.mkdir()
            subprocess.run(["git", "init", "-b", "pr", str(origin)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(origin), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(origin), "config", "user.email", "test@example.com"],
                check=True,
            )
            (origin / "tracked").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(origin), "add", "tracked"], check=True)
            subprocess.run(
                ["git", "-C", str(origin), "commit", "-m", "test"],
                check=True,
                capture_output=True,
            )
            head = subprocess.run(
                ["git", "-C", str(origin), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            checkout = joint_upgrade.ensure_git_source(
                str(origin), "pr", head, root / "checkout"
            )
            actual = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        self.assertEqual(actual, head)

    def test_targeted_attempts_override_only_covered_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "group_id": "group-42",
                "selection": {"mode": "test"},
                "prs": [
                    {
                        "repo": "repo",
                        "number": 1,
                    }
                ],
                "nodes": [
                    {
                        "key": "repo:a.spec",
                        "repo": "repo",
                        "source": "/src",
                        "spec": "a.spec",
                        "prs": ["repo#1"],
                    },
                    {
                        "key": "repo:b.spec",
                        "repo": "repo",
                        "source": "/src",
                        "spec": "b.spec",
                        "prs": ["repo#1"],
                    },
                ],
            }
            plan_path = root / "group-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            def attempt(name: str, status: str, node: str) -> Path:
                work = root / name
                work.mkdir()
                (work / "manifest.json").write_text(
                    json.dumps(
                        {
                            "group_id": "group-42",
                            "targets": [
                                {
                                    "name": name,
                                    "local_path": "/src",
                                    "specs": [node.split(":")[1]],
                                    "group_nodes": [node],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                summary = work / "summary.json"
                summary.write_text(
                    json.dumps(
                        {
                            "group_id": "group-42",
                            "attempt_id": name,
                            "items": [{"name": name, "status": status}],
                        }
                    ),
                    encoding="utf-8",
                )
                return summary

            first = attempt("first", "failed", "repo:a.spec")
            second = attempt("second", "passed", "repo:a.spec")
            third = attempt("third", "passed", "repo:b.spec")
            output = root / "group-summary.json"
            with mock.patch.object(
                sys,
                "argv",
                [
                    "assemble_group_results.py",
                    "--plan",
                    str(plan_path),
                    "--architecture",
                    "x86_64",
                    "--output",
                    str(output),
                    "--summary",
                    str(first),
                    "--summary",
                    str(second),
                    "--summary",
                    str(third),
                ],
            ):
                assemble_group_results.main()
            result = json.loads(output.read_text())
        self.assertTrue(result["allowed_to_enter_merge_train"])
        self.assertEqual(set(result["specs"].values()), {"passed"})

    def test_failed_batch_preserves_passing_sibling_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "group_id": "group-42",
                "selection": {"mode": "test"},
                "prs": [{"repo": "repo", "number": 1}],
                "nodes": [
                    {
                        "key": "repo:a.spec",
                        "repo": "repo",
                        "source": "/src",
                        "spec": "a.spec",
                        "prs": ["repo#1"],
                    },
                    {
                        "key": "repo:b.spec",
                        "repo": "repo",
                        "source": "/src",
                        "spec": "b.spec",
                        "prs": ["repo#1"],
                    },
                ],
            }
            plan_path = root / "group-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            work = root / "attempt"
            work.mkdir()
            (work / "manifest.json").write_text(
                json.dumps(
                    {
                        "group_id": "group-42",
                        "targets": [
                            {
                                "name": "mixed",
                                "group_nodes": ["repo:a.spec", "repo:b.spec"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            summary = work / "summary.json"
            summary.write_text(
                json.dumps(
                    {
                        "group_id": "group-42",
                        "architecture": "x86_64",
                        "items": [
                            {
                                "name": "mixed",
                                "status": "failed",
                                "install": {"status": "passed"},
                                "specs": [
                                    {
                                        "spec": "a.spec",
                                        "status": "failed",
                                        "tests": {"status": "failed"},
                                    },
                                    {
                                        "spec": "b.spec",
                                        "tests": {"status": "passed"},
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "result.json"
            with mock.patch.object(
                sys,
                "argv",
                [
                    "assemble_group_results.py",
                    "--plan",
                    str(plan_path),
                    "--architecture",
                    "x86_64",
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                ],
            ), self.assertRaises(SystemExit):
                assemble_group_results.main()
            result = json.loads(output.read_text())
        self.assertEqual(result["specs"]["repo:a.spec"], "failed")
        self.assertEqual(result["specs"]["repo:b.spec"], "passed")

    def test_report_policy_does_not_block_installable_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "group_id": "group-42",
                "selection": {"mode": "test"},
                "prs": [{"repo": "repo", "number": 1}],
                "nodes": [
                    {
                        "key": "repo:a.spec",
                        "repo": "repo",
                        "source": "/src",
                        "spec": "a.spec",
                        "prs": ["repo#1"],
                    }
                ],
            }
            plan_path = root / "group-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            work = root / "attempt"
            work.mkdir()
            (work / "manifest.json").write_text(
                json.dumps(
                    {
                        "group_id": "group-42",
                        "targets": [
                            {
                                "name": "target",
                                "group_nodes": ["repo:a.spec"],
                                "test_policy": "report",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            summary = work / "summary.json"
            summary.write_text(
                json.dumps(
                    {
                        "group_id": "group-42",
                        "architecture": "x86_64",
                        "items": [
                            {
                                "name": "target",
                                "status": "passed",
                                "install": {"status": "passed"},
                                "specs": [
                                    {
                                        "spec": "a.spec",
                                        "tests": {
                                            "policy": "report",
                                            "status": "failed",
                                            "enforced": False,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "result.json"
            with mock.patch.object(
                sys,
                "argv",
                [
                    "assemble_group_results.py",
                    "--plan",
                    str(plan_path),
                    "--architecture",
                    "x86_64",
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                ],
            ):
                assemble_group_results.main()
            result = json.loads(output.read_text())
        self.assertTrue(result["allowed_to_enter_merge_train"])


if __name__ == "__main__":
    unittest.main()
