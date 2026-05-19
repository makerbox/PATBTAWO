from __future__ import annotations

import dataclasses
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "asana_orchestrator", ROOT / "scripts" / "asana_orchestrator.py"
)
orchestrator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["asana_orchestrator"] = orchestrator
SPEC.loader.exec_module(orchestrator)


def make_config(tmp_path: Path, *, timeout: int | None = None) -> orchestrator.OrchestratorConfig:
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
        worktree_root=tmp_path / "worktrees",
        artifact_root=tmp_path / "artifacts",
        builder_command="builder",
        verifier_command="verifier",
        deploy_command=None,
        smoke_command=None,
        deploy_enabled=False,
        objective_verifier=True,
        keep_worktrees=False,
        stage_timeout_seconds=timeout,
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


class StageRunnerTests(unittest.TestCase):
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


class WorktreeIsolationTests(unittest.TestCase):
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


class ConfigTests(unittest.TestCase):
    def test_env_file_does_not_override_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A=from-file\nB='quoted'\n", encoding="utf-8")
            environ = {"A": "existing"}

            orchestrator.load_env_file(path, environ)

            self.assertEqual(environ["A"], "existing")
            self.assertEqual(environ["B"], "quoted")

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
