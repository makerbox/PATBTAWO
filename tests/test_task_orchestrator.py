from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "task_orchestrator", ROOT / "scripts" / "task_orchestrator.py"
)
orchestrator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["task_orchestrator"] = orchestrator
SPEC.loader.exec_module(orchestrator)


def make_config(tmp_path: Path, *, timeout: int | None = None) -> orchestrator.OrchestratorConfig:
    context_path = tmp_path / "context.md"
    context_path.write_text(
        "# Project Context\n\n## Manual Notes\n\n## Recent Work\n\n",
        encoding="utf-8",
    )
    return orchestrator.OrchestratorConfig(
        provider_name="asana",
        provider_options={"ASANA_ACCESS_TOKEN": "token"},
        project_gid="project",
        sections={
            "ready": "ready",
            "building": "building",
            "verifying": "verifying",
            "failed": "failed",
            "deploying": "deploying",
            "done": "done",
            "blocked": "blocked",
        },
        retry_limit=1,
        dry_run=True,
        base_branch="main",
        repo_root=tmp_path,
        context_path=context_path,
        worktree_root=tmp_path / "worktrees",
        artifact_root=tmp_path / "artifacts",
        builder_command="builder",
        verifier_command="verifier",
        context_update_command=None,
        planner_command=None,
        planner_tag="plan",
        deploy_command=None,
        smoke_command=None,
        deploy_enabled=False,
        objective_verifier=True,
        keep_worktrees=False,
        stage_timeout_seconds=timeout,
        stage_environment={},
    )


class ReportValidationTests(unittest.TestCase):
    def test_validate_report_accepts_minimum_shape(self) -> None:
        report = {
            "task_id": "123",
            "status": "success",
            "summary": "done",
            "changed_files": [],
            "checks": [],
            "failures": [],
            "next_action": "ship",
            "artifact_paths": [],
        }

        self.assertEqual(orchestrator.validate_report(report, "123"), [])

    def test_validate_report_rejects_missing_required_keys(self) -> None:
        failures = orchestrator.validate_report({"task_id": "123"}, "123")

        self.assertIn("report missing required key: status", failures)
        self.assertIn("report missing required key: artifact_paths", failures)

    def test_normalize_status(self) -> None:
        self.assertEqual(orchestrator.normalize_status("passed"), "success")
        self.assertEqual(orchestrator.normalize_status("external_blocker"), "blocked")
        self.assertEqual(orchestrator.normalize_status("wat"), "unknown")


class StageEnvironmentTests(unittest.TestCase):
    def test_stage_environment_includes_orchestrator_model(self) -> None:
        env = {"ORCHESTRATOR_MODEL": "gpt-5.4-mini"}

        stage_env = orchestrator.stage_environment_from_env(env)

        self.assertEqual(stage_env["ORCHESTRATOR_MODEL"], "gpt-5.4-mini")

    def test_stage_runner_exposes_shared_context_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            writer = tmp_path / "writer.py"
            writer.write_text(
                """
import json
import os

report = {
    "task_id": os.environ["ORCHESTRATOR_TASK_ID"],
    "status": "success",
    "summary": os.environ["ORCHESTRATOR_CONTEXT_PATH"],
    "changed_files": [],
    "checks": [{"name": "context path", "status": "passed"}],
    "failures": [],
    "next_action": "continue",
    "artifact_paths": [],
}
with open(os.environ["ORCHESTRATOR_REPORT_PATH"], "w", encoding="utf-8") as fh:
    json.dump(report, fh)
""".lstrip(),
                encoding="utf-8",
            )

            runner = orchestrator.StageRunner(
                dataclasses.replace(make_config(tmp_path), deploy_policy="tagged", deploy_tag="production")
            )
            outcome = runner.run(
                stage="builder",
                command=f'"{sys.executable}" "{writer}"',
                task_contract={"gid": "123", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            self.assertTrue(outcome.succeeded)
            self.assertEqual(outcome.summary, str(tmp_path / "context.md"))


class StageRunnerTests(unittest.TestCase):
    def test_deployer_skips_report_only_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            attempt_dir = tmp_path / "artifacts" / "attempt-1"
            builder_report_dir = attempt_dir / "builder"
            builder_report_dir.mkdir(parents=True)
            orchestrator.atomic_write_json(
                builder_report_dir / "builder_report.json",
                {
                    "task_id": "123",
                    "status": "success",
                    "summary": "Checked server access and reported findings.",
                    "changed_files": [],
                    "checks": [{"name": "ssh", "status": "passed"}],
                    "failures": [],
                    "next_action": "No deploy required.",
                    "artifact_paths": [],
                },
            )

            runner = orchestrator.StageRunner(
                dataclasses.replace(make_config(tmp_path), deploy_policy="tagged", deploy_tag="production")
            )
            outcome = runner.run(
                stage="deployer",
                command=f'"{sys.executable}" -c "raise SystemExit(99)"',
                task_contract={"gid": "123", "name": "Check production server connection"},
                attempt=1,
                worktree_path=tmp_path,
                attempt_artifact_dir=attempt_dir,
            )

            self.assertTrue(outcome.succeeded)
            self.assertIn("Skipped deploy/smoke", outcome.summary)
            self.assertTrue(outcome.report["deploy_skipped"])

    def test_stage_runner_accepts_valid_command_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            writer = tmp_path / "writer.py"
            writer.write_text(
                """
import json
import os

report = {
    "task_id": os.environ["ORCHESTRATOR_TASK_ID"],
    "status": "success",
    "summary": "stage passed",
    "changed_files": ["README.md"],
    "checks": [{"name": "unit", "status": "passed"}],
    "failures": [],
    "next_action": "continue",
    "artifact_paths": [],
}
with open(os.environ["ORCHESTRATOR_REPORT_PATH"], "w", encoding="utf-8") as fh:
    json.dump(report, fh)
print("hello from stage")
""".lstrip(),
                encoding="utf-8",
            )

            runner = orchestrator.StageRunner(make_config(tmp_path))
            outcome = runner.run(
                stage="builder",
                command=f'"{sys.executable}" "{writer}"',
                task_contract={"gid": "123", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            self.assertTrue(outcome.succeeded)
            self.assertEqual(outcome.status, "success")
            self.assertTrue(outcome.report_path.exists())
            self.assertTrue(outcome.log_path.exists())
            self.assertIn(str(outcome.log_path), outcome.report["artifact_paths"])

    def test_stage_runner_passes_configured_stage_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            writer = tmp_path / "writer.py"
            writer.write_text(
                """
import json
import os

report = {
    "task_id": os.environ["ORCHESTRATOR_TASK_ID"],
    "status": "success",
    "summary": os.environ["PRODUCTION_HOST"],
    "changed_files": [],
    "checks": [{"name": "deploy target", "status": "passed"}],
    "failures": [],
    "next_action": "continue",
    "artifact_paths": [],
}
with open(os.environ["ORCHESTRATOR_REPORT_PATH"], "w", encoding="utf-8") as fh:
    json.dump(report, fh)
""".lstrip(),
                encoding="utf-8",
            )

            config = dataclasses.replace(
                make_config(tmp_path),
                stage_environment={"PRODUCTION_HOST": "prod.example.com"},
            )
            runner = orchestrator.StageRunner(config)
            outcome = runner.run(
                stage="deployer",
                command=f'"{sys.executable}" "{writer}"',
                task_contract={"gid": "123", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            self.assertTrue(outcome.succeeded)
            self.assertEqual(outcome.summary, "prod.example.com")

    def test_stage_runner_expands_posix_and_windows_env_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            writer = tmp_path / "writer.py"
            writer.write_text(
                """
import json
import os
import sys

expected = os.environ["ORCHESTRATOR_TASK_CONTRACT_PATH"]
passed = sys.argv[1:]
report = {
    "task_id": os.environ["ORCHESTRATOR_TASK_ID"],
    "status": "success" if passed == [expected, expected] else "failed",
    "summary": "|".join(passed),
    "changed_files": [],
    "checks": [{"name": "env expansion", "status": "passed" if passed == [expected, expected] else "failed"}],
    "failures": [] if passed == [expected, expected] else [repr(passed), expected],
    "next_action": "continue",
    "artifact_paths": [],
}
with open(os.environ["ORCHESTRATOR_REPORT_PATH"], "w", encoding="utf-8") as fh:
    json.dump(report, fh)
sys.exit(0 if passed == [expected, expected] else 1)
""".lstrip(),
                encoding="utf-8",
            )

            runner = orchestrator.StageRunner(make_config(tmp_path))
            outcome = runner.run(
                stage="builder",
                command=f'"{sys.executable}" "{writer}" "${{ORCHESTRATOR_TASK_CONTRACT_PATH}}" "%ORCHESTRATOR_TASK_CONTRACT_PATH%"',
                task_contract={"gid": "123", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            self.assertTrue(outcome.succeeded, outcome.report)

    def test_stage_runner_synthesizes_failure_when_report_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            noop = tmp_path / "noop.py"
            noop.write_text("print('no report')\n", encoding="utf-8")

            runner = orchestrator.StageRunner(make_config(tmp_path))
            outcome = runner.run(
                stage="verifier",
                command=f'"{sys.executable}" "{noop}"',
                task_contract={"gid": "456", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            self.assertFalse(outcome.succeeded)
            self.assertEqual(outcome.status, "failed")
            self.assertTrue(outcome.report_path.exists())
            self.assertIn("missing report file", "\n".join(outcome.report["failures"]))

    def test_stage_runner_includes_command_output_when_report_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            missing = tmp_path / "missing_builder.py"

            runner = orchestrator.StageRunner(make_config(tmp_path))
            outcome = runner.run(
                stage="builder",
                command=f'"{sys.executable}" "{missing}"',
                task_contract={"gid": "789", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            failures = "\n".join(outcome.report["failures"])
            self.assertIn("stage command exited with code", failures)
            self.assertIn("stage output:", failures)
            self.assertIn("missing report file", failures)

    def test_stage_runner_preserves_valid_failed_report_with_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            writer = tmp_path / "writer.py"
            writer.write_text(
                """
import json
import os
import sys

report = {
    "task_id": os.environ["ORCHESTRATOR_TASK_ID"],
    "status": "failed",
    "summary": "Tests failed because one assertion failed.",
    "changed_files": [],
    "checks": [{"name": "unit", "status": "failed"}],
    "failures": ["expected blocked, got failed"],
    "next_action": "Fix the failing assertion.",
    "artifact_paths": [],
}
with open(os.environ["ORCHESTRATOR_REPORT_PATH"], "w", encoding="utf-8") as fh:
    json.dump(report, fh)
sys.exit(1)
""".lstrip(),
                encoding="utf-8",
            )

            runner = orchestrator.StageRunner(make_config(tmp_path))
            outcome = runner.run(
                stage="verifier",
                command=f'"{sys.executable}" "{writer}"',
                task_contract={"gid": "456", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.summary, "Tests failed because one assertion failed.")
            self.assertIn("expected blocked, got failed", outcome.report["failures"])
            self.assertNotIn("did not produce a valid passing report", outcome.summary)

    def test_stage_runner_preserves_valid_blocked_report_with_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            writer = tmp_path / "writer.py"
            writer.write_text(
                """
import json
import os
import sys

report = {
    "task_id": os.environ["ORCHESTRATOR_TASK_ID"],
    "status": "blocked",
    "summary": "Builder is not configured.",
    "changed_files": [],
    "checks": [],
    "failures": ["missing builder command"],
    "next_action": "Configure the builder command.",
    "artifact_paths": [],
}
with open(os.environ["ORCHESTRATOR_REPORT_PATH"], "w", encoding="utf-8") as fh:
    json.dump(report, fh)
sys.exit(1)
""".lstrip(),
                encoding="utf-8",
            )

            runner = orchestrator.StageRunner(make_config(tmp_path))
            outcome = runner.run(
                stage="builder",
                command=f'"{sys.executable}" "{writer}"',
                task_contract={"gid": "456", "name": "Task"},
                attempt=1,
                worktree_path=worktree,
                attempt_artifact_dir=tmp_path / "artifacts" / "attempt-1",
            )

            self.assertEqual(outcome.status, "blocked")
            self.assertTrue(outcome.blocked)
            self.assertEqual(outcome.summary, "Builder is not configured.")


class BuiltinStageAdapterTests(unittest.TestCase):
    def stage_env(self, tmp_path: Path, *, stage: str, task_id: str = "TASK-1") -> dict[str, str]:
        env = dict(os.environ)
        for key in orchestrator.PATBTAWO_RUN_COMMAND_KEYS:
            env.pop(key, None)
        env["PYTHONPATH"] = str(ROOT)
        env["ORCHESTRATOR_STAGE"] = stage
        env["ORCHESTRATOR_TASK_ID"] = task_id
        env["ORCHESTRATOR_TASK_NAME"] = "Task"
        env["ORCHESTRATOR_REPORT_PATH"] = str(tmp_path / "artifacts" / f"{stage}_report.json")
        env["ORCHESTRATOR_LOG_PATH"] = str(tmp_path / "artifacts" / f"{stage}.log")
        env["ORCHESTRATOR_ARTIFACT_DIR"] = str(tmp_path / "artifacts")
        return env

    def test_packaged_builder_writes_blocked_report_without_inner_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch.dict(os.environ, {"PATBTAWO_BUILDER_RUN_COMMAND": "echo leaked"}, clear=False):
                env = self.stage_env(tmp_path, stage="builder")

            completed = subprocess.run(
                [sys.executable, "-m", "patbtawo.builder"],
                cwd=tmp_path,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            report = json.loads(Path(env["ORCHESTRATOR_REPORT_PATH"]).read_text(encoding="utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["task_id"], "TASK-1")
            self.assertIn("PATBTAWO_BUILDER_RUN_COMMAND", report["next_action"])

    def test_packaged_builder_handles_agent_output_with_undecodable_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            noisy = tmp_path / "noisy.py"
            noisy.write_text(
                "import sys\nsys.stdout.buffer.write(b'agent output: \\x9d\\n')\n",
                encoding="utf-8",
            )
            env = self.stage_env(tmp_path, stage="builder")
            env["PATBTAWO_BUILDER_RUN_COMMAND"] = f'"{sys.executable}" "{noisy}"'

            completed = subprocess.run(
                [sys.executable, "-m", "patbtawo.builder"],
                cwd=tmp_path,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            report = json.loads(Path(env["ORCHESTRATOR_REPORT_PATH"]).read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(report["status"], "success")
            self.assertIn("agent output:", completed.stdout)

    def test_packaged_builder_expands_cross_platform_env_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            checker = tmp_path / "checker.py"
            checker.write_text(
                """
import os
import sys

expected = os.environ["ORCHESTRATOR_TASK_CONTRACT_PATH"]
sys.exit(0 if sys.argv[1:] == [expected, expected] else 1)
""".lstrip(),
                encoding="utf-8",
            )
            env = self.stage_env(tmp_path, stage="builder")
            env["ORCHESTRATOR_TASK_CONTRACT_PATH"] = str(tmp_path / "task_contract.json")
            env["PATBTAWO_BUILDER_RUN_COMMAND"] = (
                f'"{sys.executable}" "{checker}" '
                '"${ORCHESTRATOR_TASK_CONTRACT_PATH}" "%ORCHESTRATOR_TASK_CONTRACT_PATH%"'
            )

            completed = subprocess.run(
                [sys.executable, "-m", "patbtawo.builder"],
                cwd=tmp_path,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            report = json.loads(Path(env["ORCHESTRATOR_REPORT_PATH"]).read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(report["status"], "success")

    def test_packaged_context_updater_rewrites_shared_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            context_path = tmp_path / "context.md"
            context_path.write_text(
                "# Project Context\n\n## Manual Notes\n\n## Recent Work\n\n",
                encoding="utf-8",
            )
            summary_path = tmp_path / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "task_id": "TASK-1",
                        "task_name": "Ship the thing",
                        "stages": {
                            "builder": {"summary": "Built the thing"},
                            "verifier": {"summary": "Verified the thing"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            updater = tmp_path / "updater.py"
            updater.write_text(
                """
import json
import os
from pathlib import Path

summary = json.loads(Path(os.environ["ORCHESTRATOR_TASK_SUMMARY_PATH"]).read_text(encoding="utf-8"))
context_path = Path(os.environ["ORCHESTRATOR_CONTEXT_PATH"])
context = context_path.read_text(encoding="utf-8")
context += f"- {summary['task_id']}: {summary['task_name']}\\n"
context_path.write_text(context, encoding="utf-8")
""".lstrip(),
                encoding="utf-8",
            )
            env = self.stage_env(tmp_path, stage="context")
            env["ORCHESTRATOR_TASK_SUMMARY_PATH"] = str(summary_path)
            env["ORCHESTRATOR_CONTEXT_PATH"] = str(context_path)
            env["PATBTAWO_CONTEXT_UPDATE_COMMAND"] = f'"{sys.executable}" "{updater}"'

            completed = subprocess.run(
                [sys.executable, "-m", "patbtawo.context_updater"],
                cwd=tmp_path,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            report = json.loads(Path(env["ORCHESTRATOR_REPORT_PATH"]).read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(report["status"], "success")
            self.assertIn("TASK-1: Ship the thing", context_path.read_text(encoding="utf-8"))

    def test_packaged_verifier_autodetects_unittest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tests_dir = tmp_path / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "import unittest\n\nclass Sample(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            env = self.stage_env(tmp_path, stage="verifier")

            completed = subprocess.run(
                [sys.executable, "-m", "patbtawo.verifier"],
                cwd=tmp_path,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            report = json.loads(Path(env["ORCHESTRATOR_REPORT_PATH"]).read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(report["status"], "success")
            self.assertIn("unittest discover", report["checks"][0]["command"])

    def test_packaged_smoke_merges_existing_deploy_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.stage_env(tmp_path, stage="deployer")
            report_path = Path(env["ORCHESTRATOR_REPORT_PATH"])
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "task_id": "TASK-1",
                        "status": "success",
                        "summary": "deployed",
                        "changed_files": [],
                        "checks": [{"name": "deploy command", "status": "passed"}],
                        "failures": [],
                        "next_action": "smoke",
                        "artifact_paths": [],
                    }
                ),
                encoding="utf-8",
            )
            smoke = tmp_path / "smoke.py"
            smoke.write_text("print('smoke ok')\n", encoding="utf-8")
            env["PATBTAWO_SMOKE_RUN_COMMAND"] = f'"{sys.executable}" "{smoke}"'

            completed = subprocess.run(
                [sys.executable, "-m", "patbtawo.smoke"],
                cwd=tmp_path,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(report["status"], "success")
            self.assertEqual([check["name"] for check in report["checks"]], ["deploy command", "smoke command"])


class CommentFormattingTests(unittest.TestCase):
    def test_human_comment_intro_precedes_structured_payload(self) -> None:
        lines = orchestrator.human_comment_intro(
            status="failed",
            stage="verifier",
            attempt="1/2",
            summary="Tests failed.",
            next_action="Fix the failing test.",
        )

        self.assertEqual(lines[0], "PATBTAWO could not complete this task.")
        self.assertIn("Stage: verifier. Attempt: 1/2.", lines)
        self.assertIn("Summary: Tests failed.", lines)
        self.assertIn("Next: Fix the failing test.", lines)

    def test_human_comment_intro_explains_failures_with_because(self) -> None:
        lines = orchestrator.human_comment_intro(
            status="failed",
            stage="verifier",
            attempt="1/2",
            summary="Tests failed.",
            next_action="Fix the regression in the parser",
            failures=["pytest reported 3 failing parser cases"],
        )

        self.assertIn("Summary: Tests failed because pytest reported 3 failing parser cases.", lines)
        self.assertIn("Next: Fix the regression in the parser.", lines)


class ProviderTests(unittest.TestCase):
    def test_trello_provider_selects_lowest_position_card(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.params = None

            def request(self, method, url, **kwargs):
                self.params = kwargs["params"]
                return [
                    {"id": "bottom", "name": "Bottom", "pos": 200, "url": "https://trello.example/bottom"},
                    {"id": "top", "name": "Top", "pos": 100, "url": "https://trello.example/top"},
                ]

        provider = orchestrator.TrelloProvider("key", "token")
        fake_http = FakeHttp()
        provider.http = fake_http

        task = provider.get_next_ready_task("ready-list")

        self.assertEqual(task["gid"], "top")
        self.assertEqual(fake_http.params["limit"], orchestrator.TASK_PAGE_LIMIT)
        self.assertIn("pos", fake_http.params["fields"])

    def test_clickup_provider_matches_ready_status_by_id(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.requests = []

            def request(self, method, url, **kwargs):
                self.requests.append((method, url, kwargs))
                if url.endswith("/list/list-1"):
                    return {
                        "statuses": [
                            {"id": "p123_ready", "status": "Ready"},
                            {"id": "p123_done", "status": "Done"},
                        ]
                    }
                if url.endswith("/list/list-1/task"):
                    return {
                        "tasks": [
                            {
                                "id": "task-1",
                                "name": "Build feature",
                                "description": "Details",
                                "status": {"id": "p123_ready", "status": "Ready"},
                                "url": "https://app.clickup.com/t/task-1",
                            }
                        ]
                    }
                raise AssertionError(url)

        provider = orchestrator.ClickUpProvider("token", "list-1")
        fake_http = FakeHttp()
        provider.http = fake_http

        task = provider.get_next_ready_task("p123_ready")

        self.assertIsNotNone(task)
        self.assertEqual(task["gid"], "task-1")
        task_request = fake_http.requests[-1]
        self.assertEqual(task_request[2]["params"]["statuses[]"], ["Ready"])
        self.assertEqual(task_request[2]["params"]["reverse"], "true")

    def test_clickup_provider_preserves_list_api_order_by_default(self) -> None:
        class FakeHttp:
            def request(self, method, url, **kwargs):
                if url.endswith("/list/list-1"):
                    return {"statuses": [{"id": "p123_ready", "status": "Ready"}]}
                if url.endswith("/list/list-1/task"):
                    return {
                        "last_page": True,
                        "tasks": [
                            {
                                "id": "top",
                                "name": "Top",
                                "status": {"id": "p123_ready", "status": "Ready"},
                                "orderindex": "200",
                            },
                            {
                                "id": "lower-orderindex",
                                "name": "Lower orderindex",
                                "status": {"id": "p123_ready", "status": "Ready"},
                                "orderindex": "100",
                            },
                        ],
                    }
                raise AssertionError(url)

        provider = orchestrator.ClickUpProvider("token", "list-1")
        provider.http = FakeHttp()

        task = provider.get_next_ready_task("p123_ready")

        self.assertEqual(task["gid"], "top")

    def test_clickup_provider_selects_lowest_orderindex_task(self) -> None:
        class FakeHttp:
            def request(self, method, url, **kwargs):
                if url.endswith("/list/list-1"):
                    return {"statuses": [{"id": "p123_ready", "status": "Ready"}]}
                if url.endswith("/list/list-1/task"):
                    return {
                        "last_page": True,
                        "tasks": [
                            {
                                "id": "bottom",
                                "name": "Bottom",
                                "status": {"id": "p123_ready", "status": "Ready"},
                                "orderindex": "200",
                            },
                            {
                                "id": "top",
                                "name": "Top",
                                "status": {"id": "p123_ready", "status": "Ready"},
                                "orderindex": "100",
                            },
                        ],
                    }
                raise AssertionError(url)

        provider = orchestrator.ClickUpProvider("token", "list-1", task_order="orderindex")
        provider.http = FakeHttp()

        task = provider.get_next_ready_task("p123_ready")

        self.assertEqual(task["gid"], "top")

    def test_clickup_provider_can_read_ready_tasks_from_view_order(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.urls = []

            def request(self, method, url, **kwargs):
                self.urls.append(url)
                if url.endswith("/list/list-1"):
                    return {"statuses": [{"id": "p123_ready", "status": "Ready"}]}
                if url.endswith("/view/view-1/task"):
                    return {
                        "last_page": True,
                        "tasks": [
                            {
                                "id": "top",
                                "name": "Top",
                                "status": {"id": "p123_ready", "status": "Ready"},
                                "orderindex": "200",
                            },
                            {
                                "id": "bottom",
                                "name": "Bottom",
                                "status": {"id": "p123_ready", "status": "Ready"},
                                "orderindex": "100",
                            },
                        ],
                    }
                raise AssertionError(url)

        provider = orchestrator.ClickUpProvider("token", "list-1", view_id="view-1")
        fake_http = FakeHttp()
        provider.http = fake_http

        task = provider.get_next_ready_task("p123_ready")

        self.assertEqual(task["gid"], "top")
        self.assertTrue(any("/view/view-1/task" in url for url in fake_http.urls))

    def test_clickup_provider_resolves_status_id_before_move(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.update_body = None

            def request(self, method, url, **kwargs):
                if url.endswith("/list/list-1"):
                    return {"statuses": [{"id": "p123_building", "status": "Building"}]}
                if url.endswith("/task/task-1"):
                    self.update_body = kwargs["json_body"]
                    return {}
                raise AssertionError(url)

        provider = orchestrator.ClickUpProvider("token", "list-1")
        fake_http = FakeHttp()
        provider.http = fake_http

        provider.move_task("task-1", "p123_building")

        self.assertEqual(fake_http.update_body, {"status": "Building"})

    def test_jira_provider_appends_rank_order_when_missing(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.body = None

            def request(self, method, url, **kwargs):
                self.body = kwargs["json_body"]
                return {
                    "issues": [
                        {
                            "id": "10001",
                            "key": "PAT-1",
                            "fields": {"summary": "Top issue", "description": "", "status": {"name": "Ready"}},
                        }
                    ]
                }

        provider = orchestrator.JiraProvider(
            {
                "JIRA_BASE_URL": "https://example.atlassian.net",
                "JIRA_EMAIL": "user@example.com",
                "JIRA_API_TOKEN": "token",
                "JIRA_PROJECT_KEY": "PAT",
            }
        )
        fake_http = FakeHttp()
        provider.http = fake_http

        task = provider.get_next_ready_task("Ready")

        self.assertEqual(task["gid"], "PAT-1")
        self.assertTrue(fake_http.body["jql"].endswith("ORDER BY Rank ASC"))

    def test_jira_provider_preserves_explicit_ready_order(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.body = None

            def request(self, method, url, **kwargs):
                self.body = kwargs["json_body"]
                return {"issues": []}

        provider = orchestrator.JiraProvider(
            {
                "JIRA_BASE_URL": "https://example.atlassian.net",
                "JIRA_EMAIL": "user@example.com",
                "JIRA_API_TOKEN": "token",
                "JIRA_READY_JQL": "project = PAT AND status = Ready ORDER BY priority DESC",
            }
        )
        fake_http = FakeHttp()
        provider.http = fake_http

        provider.get_next_ready_task("Ready")

        self.assertEqual(fake_http.body["jql"], "project = PAT AND status = Ready ORDER BY priority DESC")


class OrchestratorQueueTests(unittest.TestCase):
    def test_run_continues_after_task_failure_when_not_once(self) -> None:
        class FakeProvider:
            def __init__(self) -> None:
                self.ready = [
                    {"gid": "task-1", "name": "First"},
                    {"gid": "task-2", "name": "Second"},
                ]
                self.moved = []
                self.comments = []

            def get_next_ready_task(self, section_gid):
                return self.ready.pop(0) if self.ready else None

            def get_task_contract(self, task_gid):
                return {"gid": task_gid, "name": task_gid}

            def move_task(self, task_gid, section_gid):
                self.moved.append((task_gid, section_gid))

            def comment(self, task_gid, text):
                self.comments.append((task_gid, text))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = make_config(tmp_path)
            provider = FakeProvider()
            task_orchestrator = orchestrator.TaskOrchestrator(config, provider)

            outcomes = [
                orchestrator.StageOutcome(
                    stage="builder",
                    status="failed",
                    summary="Builder failed",
                    report={
                        "next_action": "Fix builder",
                        "failures": ["boom"],
                    },
                    report_path=tmp_path / "one.json",
                    log_path=tmp_path / "one.log",
                    retryable=False,
                    exit_code=1,
                ),
                orchestrator.StageOutcome(
                    stage="builder",
                    status="failed",
                    summary="Builder failed again",
                    report={
                        "next_action": "Fix builder",
                        "failures": ["boom"],
                    },
                    report_path=tmp_path / "two.json",
                    log_path=tmp_path / "two.log",
                    retryable=False,
                    exit_code=1,
                ),
            ]

            def create_worktree(task_id, attempt, stage="builder", base_ref=None):
                return tmp_path / f"{task_id}-{attempt}", f"branch-{task_id}"

            task_orchestrator.worktrees.create = create_worktree
            task_orchestrator.worktrees.remove = lambda path: None

            def run_attempt(task_contract, attempt, attempt_dir, worktree_path):
                return outcomes.pop(0)

            task_orchestrator._run_attempt = run_attempt

            task_orchestrator.run(once=False)

            failed_moves = [move for move in provider.moved if move[1] == config.sections["failed"]]
            self.assertEqual(failed_moves, [("task-1", "failed"), ("task-2", "failed")])
            self.assertEqual(outcomes, [])

    def test_run_attempt_refreshes_shared_context_before_merge(self) -> None:
        class FakeProvider:
            def __init__(self) -> None:
                self.moves = []
                self.comments = []

            def move_task(self, task_gid, section_gid):
                self.moves.append((task_gid, section_gid))

            def comment(self, task_gid, text):
                self.comments.append((task_gid, text))

        class FakeRunner:
            def run(
                self,
                *,
                stage,
                command,
                task_contract,
                attempt,
                worktree_path,
                attempt_artifact_dir,
                extra_env=None,
            ):
                report_path = attempt_artifact_dir / stage / f"{stage}_report.json"
                log_path = attempt_artifact_dir / stage / f"{stage}.log"
                report_path.parent.mkdir(parents=True, exist_ok=True)
                if stage == "context":
                    context_path = worktree_path / "context.md"
                    context_path.write_text(
                        "# Project Context\n\n## Manual Notes\n\n## Recent Work\n\n- task-1 completed\n",
                        encoding="utf-8",
                    )
                report = {
                    "task_id": task_contract["gid"],
                    "status": "success",
                    "summary": f"{stage} completed",
                    "changed_files": ["context.md"] if stage == "context" else [],
                    "checks": [{"name": stage, "status": "passed"}],
                    "failures": [],
                    "next_action": "continue",
                    "artifact_paths": [str(log_path)],
                }
                report_path.write_text(json.dumps(report), encoding="utf-8")
                log_path.write_text(f"{stage} log\n", encoding="utf-8")
                return orchestrator.StageOutcome(
                    stage=stage,
                    status="success",
                    summary=f"{stage} completed",
                    report=report,
                    report_path=report_path,
                    log_path=log_path,
                    retryable=False,
                    exit_code=0,
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            (repo / "context.md").write_text("# Project Context\n\n## Manual Notes\n\n## Recent Work\n\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md", "context.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            config = dataclasses.replace(
                make_config(tmp_path),
                repo_root=repo,
                context_path=repo / "context.md",
                worktree_root=tmp_path / "worktrees",
                artifact_root=tmp_path / "artifacts",
                base_branch="main",
                objective_verifier=False,
                deploy_enabled=False,
                context_update_command="python -m patbtawo.context_updater",
            )
            provider = FakeProvider()
            task_orchestrator = orchestrator.TaskOrchestrator(config, provider)
            task_orchestrator.runner = FakeRunner()

            worktree_path, _ = task_orchestrator.worktrees.create("task-1", 1, stage="builder")
            try:
                outcome = task_orchestrator._run_attempt(
                    {"gid": "task-1", "name": "Task 1"},
                    1,
                    tmp_path / "artifacts" / "attempt-1",
                    worktree_path,
                )
            finally:
                task_orchestrator.worktrees.remove(worktree_path)

            self.assertTrue(outcome.succeeded)
            self.assertEqual(outcome.stage, "context")
            self.assertIn(("task-1", config.sections["done"]), provider.moves)
            self.assertIn("- task-1 completed", (repo / "context.md").read_text(encoding="utf-8"))

    def test_planning_task_creates_ready_tasks_and_marks_source_done(self) -> None:
        class FakeProvider:
            def __init__(self) -> None:
                self.created = []
                self.moves = []
                self.comments = []

            def create_task(self, *, title, description, ready_state, tags=(), parent_task_id=""):
                self.created.append(
                    {
                        "title": title,
                        "description": description,
                        "ready_state": ready_state,
                        "tags": list(tags),
                        "parent_task_id": parent_task_id,
                    }
                )
                return {
                    "gid": f"created-{len(self.created)}",
                    "name": title,
                    "permalink_url": f"https://example.invalid/{len(self.created)}",
                }

            def move_task(self, task_gid, section_gid):
                self.moves.append((task_gid, section_gid))

            def comment(self, task_gid, text):
                self.comments.append((task_gid, text))

        class FakeRunner:
            def run(
                self,
                *,
                stage,
                command,
                task_contract,
                attempt,
                worktree_path,
                attempt_artifact_dir,
                extra_env=None,
            ):
                report_path = attempt_artifact_dir / stage / f"{stage}_report.json"
                log_path = attempt_artifact_dir / stage / f"{stage}.log"
                report_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path = Path((extra_env or {})["ORCHESTRATOR_TASK_PLAN_PATH"])
                orchestrator.atomic_write_json(
                    plan_path,
                    {
                        "tasks": [
                            {
                                "title": "Build route",
                                "description": "Add the public route.",
                                "acceptance_criteria": ["Route returns 200"],
                                "tags": ["deploy"],
                            }
                        ]
                    },
                )
                report = {
                    "task_id": task_contract["gid"],
                    "status": "success",
                    "summary": "planned",
                    "changed_files": [],
                    "checks": [{"name": "plan", "status": "passed"}],
                    "failures": [],
                    "next_action": "create tasks",
                    "artifact_paths": [str(log_path), str(plan_path)],
                }
                orchestrator.atomic_write_json(report_path, report)
                log_path.write_text("planner log\n", encoding="utf-8")
                return orchestrator.StageOutcome(
                    stage=stage,
                    status="success",
                    summary="planned",
                    report=report,
                    report_path=report_path,
                    log_path=log_path,
                    retryable=False,
                    exit_code=0,
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            config = dataclasses.replace(
                make_config(tmp_path),
                planner_command="python -m patbtawo.planner",
                planner_tag="plan",
            )
            provider = FakeProvider()
            task_orchestrator = orchestrator.TaskOrchestrator(config, provider)
            task_orchestrator.runner = FakeRunner()

            outcome = task_orchestrator._run_attempt(
                {
                    "gid": "plan-1",
                    "name": "Break this goal into deployable chunks",
                    "notes": "Create a task for each chunk and tag them deploy.",
                    "tags": [{"name": "plan"}],
                    "permalink_url": "https://example.invalid/plan-1",
                },
                1,
                tmp_path / "artifacts" / "attempt-1",
                worktree,
            )

            self.assertTrue(outcome.succeeded)
            self.assertEqual(outcome.stage, "planner")
            self.assertEqual(provider.created[0]["ready_state"], config.sections["ready"])
            self.assertEqual(provider.created[0]["tags"], ["deploy"])
            self.assertIn("Acceptance criteria", provider.created[0]["description"])
            self.assertIn(("plan-1", config.sections["done"]), provider.moves)
            self.assertIn("created_tasks", outcome.report)


class WorktreeIsolationTests(unittest.TestCase):
    def test_remove_handles_read_only_worktree_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = make_config(tmp_path)
            manager = orchestrator.WorktreeManager(config)
            worktree = config.worktree_root / "task-attempt-1-builder"
            docs = worktree / "docs"
            docs.mkdir(parents=True)
            read_only_file = docs / "note.txt"
            read_only_file.write_text("locked down\n", encoding="utf-8")
            os.chmod(read_only_file, stat.S_IREAD)

            manager.remove(worktree)

            self.assertFalse(worktree.exists())

    def test_remove_warns_instead_of_crashing_when_worktree_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = make_config(tmp_path)
            manager = orchestrator.WorktreeManager(config)
            worktree = config.worktree_root / "task-attempt-1-builder"
            worktree.mkdir(parents=True)

            with mock.patch.object(orchestrator.shutil, "rmtree", side_effect=PermissionError("locked")):
                with mock.patch.object(orchestrator.time, "sleep"):
                    with mock.patch("builtins.print") as print_mock:
                        manager.remove(worktree)

            self.assertTrue(worktree.exists())
            printed = "\n".join(" ".join(str(part) for part in call.args) for call in print_mock.call_args_list)
            self.assertIn("Warning: could not remove worktree", printed)

    def test_checkpoint_can_seed_fresh_verifier_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            config = dataclasses.replace(make_config(tmp_path), repo_root=repo, base_branch="HEAD")
            manager = orchestrator.WorktreeManager(config)

            builder_path, _ = manager.create("TASK-1", 1, stage="builder")
            (builder_path / "feature.txt").write_text("built\n", encoding="utf-8")
            ref = manager.checkpoint(builder_path, task_id="TASK-1", attempt=1, stage="builder")
            verifier_path, _ = manager.create("TASK-1", 1, stage="verifier", base_ref=ref)

            self.assertEqual((verifier_path / "feature.txt").read_text(encoding="utf-8"), "built\n")

            (verifier_path / "README.md").write_text("mutated by verifier\n", encoding="utf-8")
            self.assertEqual(orchestrator.tracked_changed_files(verifier_path), ["README.md"])

            manager.remove(verifier_path)
            manager.remove(builder_path)

    def test_merge_into_base_and_remove_cleans_up_attempt_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            config = dataclasses.replace(make_config(tmp_path), repo_root=repo, base_branch="main")
            manager = orchestrator.WorktreeManager(config)

            builder_path, branch = manager.create("TASK-1", 1, stage="builder")
            (builder_path / "feature.txt").write_text("built\n", encoding="utf-8")
            ref = manager.checkpoint(builder_path, task_id="TASK-1", attempt=1, stage="builder")

            manager.merge_into_base(ref)
            self.assertEqual((repo / "feature.txt").read_text(encoding="utf-8"), "built\n")

            manager.remove(builder_path)
            branch_check = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertNotEqual(branch_check.returncode, 0)

    def test_reset_attempts_removes_state_artifacts_worktrees_and_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            worktree_root = tmp_path / "worktrees"
            artifact_root = tmp_path / "artifacts"
            worktree_root.mkdir()
            artifact_root.mkdir()
            config = orchestrator.AttemptResetConfig(
                repo_root=repo.resolve(),
                worktree_root=worktree_root.resolve(),
                artifact_root=artifact_root.resolve(),
            )

            branch = "orchestrator/TASK-1-attempt-1-builder-20260521T000000000000Z"
            git_worktree = worktree_root / "TASK-1-attempt-1-builder-20260521T000000000000Z"
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(git_worktree), "HEAD"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stale_worktree = worktree_root / "TASK-2-attempt-1-builder-20260521T000000000000Z"
            stale_worktree.mkdir()
            kept_worktree_dir = worktree_root / "manual-checkout"
            kept_worktree_dir.mkdir()

            attempt_dir = artifact_root / "TASK-1" / "attempt-001-20260521T000000000000Z"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "builder.log").write_text("log\n", encoding="utf-8")
            kept_artifact = artifact_root / "notes.txt"
            kept_artifact.write_text("keep\n", encoding="utf-8")
            orchestrator.atomic_write_json(
                artifact_root / "orchestrator_state.json",
                {"active_attempt": {"task_id": "TASK-1", "worktree_path": str(git_worktree)}},
            )

            counts = orchestrator.reset_attempts(config)

            self.assertEqual(counts["state_files"], 1)
            self.assertEqual(counts["worktrees"], 1)
            self.assertEqual(counts["stale_worktree_dirs"], 1)
            self.assertEqual(counts["branches"], 1)
            self.assertEqual(counts["artifact_dirs"], 1)
            self.assertFalse(git_worktree.exists())
            self.assertFalse(stale_worktree.exists())
            self.assertTrue(kept_worktree_dir.exists())
            self.assertFalse(attempt_dir.exists())
            self.assertTrue(kept_artifact.exists())
            self.assertNotIn("active_attempt", orchestrator.read_json(artifact_root / "orchestrator_state.json"))

            branch_check = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertNotEqual(branch_check.returncode, 0)


class ConfigTests(unittest.TestCase):
    def test_task_has_deploy_tag_reads_clickup_tags(self) -> None:
        task_contract = {
            "gid": "task-1",
            "raw": {"tags": [{"name": "Production"}, {"name": "other"}]},
        }

        self.assertTrue(orchestrator.task_has_deploy_tag(task_contract, "production"))
        self.assertFalse(orchestrator.task_has_deploy_tag(task_contract, "staging"))

    def test_normalize_deploy_policy_keeps_backward_enabled_flag(self) -> None:
        self.assertEqual(
            orchestrator.normalize_deploy_policy(None, deploy_enabled_value="true", has_deploy_command=False),
            "all",
        )
        self.assertEqual(
            orchestrator.normalize_deploy_policy(None, deploy_enabled_value="false", has_deploy_command=True),
            "none",
        )
        self.assertEqual(
            orchestrator.normalize_deploy_policy("tagged-only", deploy_enabled_value=None, has_deploy_command=True),
            "tagged",
        )

    def test_env_file_does_not_override_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A=from-file\nB='quoted'\n", encoding="utf-8")
            environ = {"A": "existing"}

            orchestrator.load_env_file(path, environ)

            self.assertEqual(environ["A"], "existing")
            self.assertEqual(environ["B"], "quoted")

    def test_configuration_help_mentions_provider_and_module_command(self) -> None:
        message = orchestrator.configuration_help(
            "asana",
            orchestrator.MissingConfigError("Missing required configuration: ASANA_ACCESS_TOKEN"),
        )

        self.assertIn("Selected provider: asana", message)
        self.assertIn("ASANA_ACCESS_TOKEN", message)
        self.assertIn("python -m patbtawo --validate-config --print-config", message)

    def test_ensure_under_rejects_root_and_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            child = root / "child"
            root.mkdir()
            child.mkdir()

            orchestrator.ensure_under(child, root)
            with self.assertRaises(orchestrator.ConfigError):
                orchestrator.ensure_under(root, root)
            with self.assertRaises(orchestrator.ConfigError):
                orchestrator.ensure_under(Path(tmp) / "outside", root)

    def test_config_from_env_discovers_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            env = {
                "ASANA_ACCESS_TOKEN": "token",
                "ASANA_PROJECT_GID": "project",
                "ASANA_SECTION_READY_GID": "ready",
                "ASANA_SECTION_BUILDING_GID": "building",
                "ASANA_SECTION_VERIFYING_GID": "verifying",
                "ASANA_SECTION_FAILED_GID": "failed",
                "ASANA_SECTION_DEPLOYING_GID": "deploying",
                "ASANA_SECTION_DONE_GID": "done",
                "ASANA_SECTION_BLOCKED_GID": "blocked",
                "ORCHESTRATOR_BUILDER_COMMAND": "build",
                "ORCHESTRATOR_VERIFIER_COMMAND": "verify",
                "ORCHESTRATOR_RETRY_LIMIT": "2",
            }

            config = orchestrator.OrchestratorConfig.from_env(env, cwd=repo)

            self.assertEqual(config.repo_root, repo.resolve())
            self.assertEqual(config.provider_name, "asana")
            self.assertEqual(config.retry_limit, 2)
            self.assertFalse(config.deploy_enabled)

    def test_reset_attempts_cli_does_not_require_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                self.assertEqual(orchestrator.main(["--reset-attempts", "--dry-run"]), 0)
            finally:
                os.chdir(old_cwd)

    def test_config_from_env_collects_stage_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            env = {
                "ASANA_ACCESS_TOKEN": "token",
                "ASANA_PROJECT_GID": "project",
                "ASANA_SECTION_READY_GID": "ready",
                "ASANA_SECTION_BUILDING_GID": "building",
                "ASANA_SECTION_VERIFYING_GID": "verifying",
                "ASANA_SECTION_FAILED_GID": "failed",
                "ASANA_SECTION_DEPLOYING_GID": "deploying",
                "ASANA_SECTION_DONE_GID": "done",
                "ASANA_SECTION_BLOCKED_GID": "blocked",
                "ORCHESTRATOR_BUILDER_COMMAND": "build",
                "ORCHESTRATOR_VERIFIER_COMMAND": "verify",
                "ORCHESTRATOR_DEPLOY_HOST": "prod.example.com",
                "ORCHESTRATOR_DEPLOY_USER": "deploy",
                "ORCHESTRATOR_STAGE_ENV_KEYS": "PRODUCTION_HOST",
                "PRODUCTION_HOST": "app.example.com",
                "ORCHESTRATOR_STAGE_ENV_DEPLOY_PORT": "22",
                "PATBTAWO_BUILDER_RUN_COMMAND": "codex exec task",
                "PATBTAWO_VERIFIER_RUN_COMMAND": "python -m unittest",
            }

            config = orchestrator.OrchestratorConfig.from_env(env, cwd=repo)

            self.assertEqual(config.stage_environment["ORCHESTRATOR_DEPLOY_HOST"], "prod.example.com")
            self.assertEqual(config.stage_environment["ORCHESTRATOR_DEPLOY_USER"], "deploy")
            self.assertEqual(config.stage_environment["PRODUCTION_HOST"], "app.example.com")
            self.assertEqual(config.stage_environment["DEPLOY_PORT"], "22")
            self.assertEqual(config.stage_environment["PATBTAWO_BUILDER_RUN_COMMAND"], "codex exec task")
            self.assertEqual(config.stage_environment["PATBTAWO_VERIFIER_RUN_COMMAND"], "python -m unittest")
            self.assertNotIn("ASANA_ACCESS_TOKEN", config.stage_environment)

    def test_validate_runtime_config_rejects_missing_stage_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = dataclasses.replace(
                make_config(tmp_path),
                builder_command="python scripts/builder.py",
            )

            with self.assertRaises(orchestrator.ConfigError) as context:
                orchestrator.validate_runtime_config(config)

            message = str(context.exception)
            self.assertIn("builder command references missing local path", message)
            self.assertIn("scripts/builder.py", message)

    def test_validate_runtime_config_rejects_missing_context_file_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = make_config(tmp_path)
            context_path = tmp_path / "context.md"
            context_path.unlink()
            config = dataclasses.replace(
                config,
                context_path=context_path,
                context_update_command="python -m patbtawo.context_updater",
            )

            with self.assertRaises(orchestrator.ConfigError) as context:
                orchestrator.validate_runtime_config(config)

            self.assertIn("Missing shared context file", str(context.exception))

    def test_validate_runtime_config_rejects_misordered_codex_approval_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = dataclasses.replace(
                make_config(tmp_path),
                stage_environment={
                    "PATBTAWO_BUILDER_RUN_COMMAND": (
                        "codex exec --sandbox workspace-write --ask-for-approval never "
                        "\"Do the task\""
                    )
                },
            )

            with self.assertRaises(orchestrator.ConfigError) as context:
                orchestrator.validate_runtime_config(config)

            self.assertIn("codex --ask-for-approval never exec", str(context.exception))

    def test_validate_runtime_config_accepts_top_level_codex_approval_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = dataclasses.replace(
                make_config(tmp_path),
                stage_environment={
                    "PATBTAWO_BUILDER_RUN_COMMAND": (
                        "codex --ask-for-approval never exec --sandbox workspace-write "
                        "\"Do the task\""
                    )
                },
            )

            orchestrator.validate_runtime_config(config)

    def test_trello_config_uses_list_state_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            env = {
                "ORCHESTRATOR_PROVIDER": "trello",
                "TRELLO_API_KEY": "key",
                "TRELLO_TOKEN": "token",
                "TRELLO_LIST_READY_ID": "ready-list",
                "TRELLO_LIST_BUILDING_ID": "building-list",
                "TRELLO_LIST_VERIFYING_ID": "verifying-list",
                "TRELLO_LIST_FAILED_ID": "failed-list",
                "TRELLO_LIST_DEPLOYING_ID": "deploying-list",
                "TRELLO_LIST_DONE_ID": "done-list",
                "TRELLO_LIST_BLOCKED_ID": "blocked-list",
                "ORCHESTRATOR_BUILDER_COMMAND": "build",
                "ORCHESTRATOR_VERIFIER_COMMAND": "verify",
            }

            config = orchestrator.OrchestratorConfig.from_env(env, cwd=repo)

            self.assertEqual(config.provider_name, "trello")
            self.assertEqual(config.sections["ready"], "ready-list")
            self.assertEqual(config.provider_options["TRELLO_API_KEY"], "key")

    def test_clickup_config_keeps_optional_view_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            env = {
                "ORCHESTRATOR_PROVIDER": "clickup",
                "CLICKUP_ACCESS_TOKEN": "token",
                "CLICKUP_LIST_ID": "list-1",
                "CLICKUP_VIEW_ID": "view-1",
                "ORCHESTRATOR_STATE_READY_ID": "Ready",
                "ORCHESTRATOR_STATE_BUILDING_ID": "Building",
                "ORCHESTRATOR_STATE_VERIFYING_ID": "Verifying",
                "ORCHESTRATOR_STATE_FAILED_ID": "Failed",
                "ORCHESTRATOR_STATE_DEPLOYING_ID": "Deploying",
                "ORCHESTRATOR_STATE_DONE_ID": "Done",
                "ORCHESTRATOR_STATE_BLOCKED_ID": "Blocked",
                "ORCHESTRATOR_BUILDER_COMMAND": "build",
                "ORCHESTRATOR_VERIFIER_COMMAND": "verify",
            }

            config = orchestrator.OrchestratorConfig.from_env(env, cwd=repo)

            self.assertEqual(config.provider_options["CLICKUP_VIEW_ID"], "view-1")

    def test_clickup_config_keeps_order_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            env = {
                "ORCHESTRATOR_PROVIDER": "clickup",
                "CLICKUP_ACCESS_TOKEN": "token",
                "CLICKUP_LIST_ID": "list-1",
                "CLICKUP_TASK_ORDER": "orderindex",
                "CLICKUP_TASK_REVERSE": "false",
                "ORCHESTRATOR_STATE_READY_ID": "Ready",
                "ORCHESTRATOR_STATE_BUILDING_ID": "Building",
                "ORCHESTRATOR_STATE_VERIFYING_ID": "Verifying",
                "ORCHESTRATOR_STATE_FAILED_ID": "Failed",
                "ORCHESTRATOR_STATE_DEPLOYING_ID": "Deploying",
                "ORCHESTRATOR_STATE_DONE_ID": "Done",
                "ORCHESTRATOR_STATE_BLOCKED_ID": "Blocked",
                "ORCHESTRATOR_BUILDER_COMMAND": "build",
                "ORCHESTRATOR_VERIFIER_COMMAND": "verify",
            }

            config = orchestrator.OrchestratorConfig.from_env(env, cwd=repo)

            self.assertEqual(config.provider_options["CLICKUP_TASK_ORDER"], "orderindex")
            self.assertEqual(config.provider_options["CLICKUP_TASK_REVERSE"], "false")

    def test_command_provider_normalizes_arbitrary_payload(self) -> None:
        provider = orchestrator.CommandProvider(
            {
                "ORCHESTRATOR_COMMAND_NEXT_TASK": "",
                "ORCHESTRATOR_COMMAND_GET_TASK": "",
                "ORCHESTRATOR_COMMAND_MOVE_TASK": "",
                "ORCHESTRATOR_COMMAND_COMMENT_TASK": "",
            },
            {"ready": "ready"},
            dry_run=True,
        )

        payload = provider._normalize_payload({"id": "abc", "title": "Do it", "description": "Details"})

        self.assertEqual(payload["gid"], "abc")
        self.assertEqual(payload["name"], "Do it")
        self.assertEqual(payload["provider"], "command")


if __name__ == "__main__":
    unittest.main()
