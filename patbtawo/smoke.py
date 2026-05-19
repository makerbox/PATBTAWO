"""Built-in PATBTAWO smoke stage."""

from __future__ import annotations

from .stage_adapter import (
    command_from_env,
    load_existing_report,
    result_failures,
    result_status,
    run_shell,
    run_url_smoke,
    smoke_url,
    write_report,
)


def main() -> int:
    command = command_from_env(["PATBTAWO_SMOKE_RUN_COMMAND"])
    previous = load_existing_report()
    previous_checks = previous.get("checks") if isinstance(previous.get("checks"), list) else []
    previous_artifacts = previous.get("artifact_paths") if isinstance(previous.get("artifact_paths"), list) else []

    if command:
        result = run_shell(command, log_name="smoke_command.log")
        status = result_status(int(result["exit_code"]))
        failures = result_failures(result)
        checks = [
            *previous_checks,
            {
                "name": "smoke command",
                "status": "passed" if status == "success" else "failed",
                "command": command,
                "exit_code": result["exit_code"],
                "duration_seconds": result["duration_seconds"],
            },
        ]
        write_report(
            stage="deployer",
            status=status,
            summary="Smoke command completed successfully." if status == "success" else "Smoke command failed.",
            checks=checks,
            failures=failures,
            next_action="Move to Done." if status == "success" else "Fix the deployed service or smoke check.",
            artifact_paths=[*previous_artifacts, result["log_path"]],
        )
        return 0 if status == "success" else int(result["exit_code"] or 1)

    url = smoke_url()
    if not url:
        write_report(
            stage="deployer",
            status="blocked",
            summary="Smoke check is not configured.",
            checks=previous_checks,
            failures=["No PATBTAWO_SMOKE_RUN_COMMAND, ORCHESTRATOR_SMOKE_URL, or ORCHESTRATOR_DEPLOY_URL was set."],
            next_action="Set a smoke command or URL so PATBTAWO can verify the deployed application.",
            artifact_paths=previous_artifacts,
        )
        return 1

    result = run_url_smoke(url)
    passed = bool(result["passed"])
    checks = [
        *previous_checks,
        {
            "name": "smoke url",
            "status": "passed" if passed else "failed",
            "url": url,
            "status_code": result["status_code"],
            "duration_seconds": result["duration_seconds"],
        },
    ]
    write_report(
        stage="deployer",
        status="success" if passed else "failed",
        summary=f"Smoke URL check passed for {url}." if passed else f"Smoke URL check failed for {url}.",
        checks=checks,
        failures=[] if passed else [result["failure"] or f"Unexpected status code: {result['status_code']}"],
        next_action="Move to Done." if passed else "Fix the deployed service or update the smoke URL.",
        artifact_paths=previous_artifacts,
        extra={"smoke_url": url},
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
