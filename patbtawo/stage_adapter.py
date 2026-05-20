"""Built-in PATBTAWO stage adapters.

These adapters let a repository use packaged stage commands that always write
the required JSON report. Repo-specific tools can be plugged in with
PATBTAWO_*_RUN_COMMAND environment variables.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib import error, request


SUCCESS_STATUSES = {"success", "passed"}
TRANSIENT_EXIT_CODES = {75}
PROCESS_ENCODING = "utf-8"
PROCESS_ERRORS = "replace"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def task_id() -> str:
    return env("ORCHESTRATOR_TASK_ID", "unknown")


def stage_name(default: str) -> str:
    return env("ORCHESTRATOR_STAGE", default) or default


def report_path() -> Path:
    path = env("ORCHESTRATOR_REPORT_PATH")
    if not path:
        raise RuntimeError("ORCHESTRATOR_REPORT_PATH is required")
    return Path(path)


def artifact_dir() -> Path:
    path = env("ORCHESTRATOR_ARTIFACT_DIR")
    if path:
        return Path(path)
    return report_path().parent


def orchestrator_log_path() -> str:
    return env("ORCHESTRATOR_LOG_PATH")


def changed_files() -> List[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            text=True,
            encoding=PROCESS_ENCODING,
            errors=PROCESS_ERRORS,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []

    files: List[str] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path)
    return sorted(set(files))


def write_report(
    *,
    stage: str,
    status: str,
    summary: str,
    checks: Sequence[Mapping[str, Any]] | None = None,
    failures: Sequence[Any] | None = None,
    next_action: str = "",
    artifact_paths: Iterable[str | Path] = (),
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    paths = [str(path) for path in artifact_paths if str(path)]
    log_path = orchestrator_log_path()
    if log_path and log_path not in paths:
        paths.append(log_path)
    report: Dict[str, Any] = {
        "task_id": task_id(),
        "status": status,
        "summary": summary,
        "changed_files": changed_files(),
        "checks": list(checks or []),
        "failures": list(failures or []),
        "next_action": next_action,
        "artifact_paths": paths,
        "stage": stage,
        "finished_at": utc_now(),
    }
    if extra:
        report.update(dict(extra))
    path = report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def load_existing_report() -> Dict[str, Any]:
    path = report_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def command_from_env(names: Sequence[str]) -> str:
    for name in names:
        value = env(name)
        if value:
            return value
    return ""


def write_stdout(text: str) -> None:
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or PROCESS_ENCODING
        sys.stdout.buffer.write(text.encode(encoding, errors=PROCESS_ERRORS))
    sys.stdout.flush()


def run_shell(command: str, *, log_name: str) -> Dict[str, Any]:
    artifact_dir().mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir() / log_name
    started = time.monotonic()
    completed = subprocess.run(
        command,
        shell=True,
        text=True,
        encoding=PROCESS_ENCODING,
        errors=PROCESS_ERRORS,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    duration = time.monotonic() - started
    output = completed.stdout or ""
    write_stdout(output)
    log_path.write_text(
        "\n".join(
            [
                f"command={command}",
                f"started_at={utc_now()}",
                output,
                f"finished_at={utc_now()}",
                f"duration_seconds={duration:.3f}",
                f"exit_code={completed.returncode}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "output": output,
        "log_path": log_path,
        "duration_seconds": round(duration, 3),
    }


def result_status(exit_code: int) -> str:
    return "success" if exit_code == 0 else "failed"


def result_failures(result: Mapping[str, Any]) -> List[str]:
    if int(result.get("exit_code", 1)) == 0:
        return []
    output = " ".join(str(result.get("output", "")).split())
    if len(output) > 500:
        output = "..." + output[-500:]
    if output:
        return [f"command exited with code {result.get('exit_code')}: {output}"]
    return [f"command exited with code {result.get('exit_code')}"]


def run_command_stage(
    *,
    stage: str,
    command: str,
    missing_status: str,
    missing_summary: str,
    missing_next_action: str,
    success_summary: str,
    failure_summary: str,
    log_name: str,
) -> int:
    if not command:
        write_report(
            stage=stage,
            status=missing_status,
            summary=missing_summary,
            checks=[],
            failures=[missing_summary],
            next_action=missing_next_action,
        )
        return 0 if missing_status in SUCCESS_STATUSES else 1

    result = run_shell(command, log_name=log_name)
    status = result_status(int(result["exit_code"]))
    failures = result_failures(result)
    summary = success_summary if status == "success" else failure_summary
    next_action = "Continue to the next stage." if status == "success" else "Inspect the stage log and fix the command failure."
    write_report(
        stage=stage,
        status=status,
        summary=summary,
        checks=[
            {
                "name": f"{stage} command",
                "status": "passed" if status == "success" else "failed",
                "command": command,
                "exit_code": result["exit_code"],
                "duration_seconds": result["duration_seconds"],
            }
        ],
        failures=failures,
        next_action=next_action,
        artifact_paths=[result["log_path"]],
        extra={"retryable": int(result["exit_code"]) in TRANSIENT_EXIT_CODES},
    )
    return 0 if status == "success" else int(result["exit_code"] or 1)


def detect_verifier_command() -> str:
    explicit = command_from_env(["PATBTAWO_VERIFIER_RUN_COMMAND", "PATBTAWO_VERIFY_RUN_COMMAND"])
    if explicit:
        return explicit
    if Path("tests").exists():
        return f'"{sys.executable}" -m unittest discover -s tests'
    if any(Path(".").glob("**/*.py")):
        return f'"{sys.executable}" -m compileall -q .'
    return ""


def smoke_url() -> str:
    return command_from_env(["ORCHESTRATOR_SMOKE_URL", "ORCHESTRATOR_DEPLOY_URL", "ORCHESTRATOR_DEPLOY_PUBLIC_URL"])


def run_url_smoke(url: str) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        req = request.Request(url, headers={"User-Agent": "PATBTAWO smoke check"})
        with request.urlopen(req, timeout=30) as response:
            body = response.read(2048)
            status_code = int(getattr(response, "status", response.getcode()))
        duration = time.monotonic() - started
        return {
            "url": url,
            "status_code": status_code,
            "passed": 200 <= status_code < 400,
            "body_sample": body.decode("utf-8", errors="replace")[:300],
            "duration_seconds": round(duration, 3),
            "failure": "",
        }
    except error.HTTPError as exc:
        duration = time.monotonic() - started
        return {
            "url": url,
            "status_code": exc.code,
            "passed": False,
            "body_sample": "",
            "duration_seconds": round(duration, 3),
            "failure": f"HTTP {exc.code}: {exc.reason}",
        }
    except Exception as exc:
        duration = time.monotonic() - started
        return {
            "url": url,
            "status_code": None,
            "passed": False,
            "body_sample": "",
            "duration_seconds": round(duration, 3),
            "failure": str(exc),
        }
