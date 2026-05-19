"""Built-in PATBTAWO deployer stage."""

from __future__ import annotations

from .stage_adapter import command_from_env, run_command_stage


def main() -> int:
    command = command_from_env(["PATBTAWO_DEPLOY_RUN_COMMAND"])
    return run_command_stage(
        stage="deployer",
        command=command,
        missing_status="blocked",
        missing_summary="Deployer is not configured.",
        missing_next_action="Set PATBTAWO_DEPLOY_RUN_COMMAND to the repository's deploy command.",
        success_summary="Deploy command completed successfully.",
        failure_summary="Deploy command failed.",
        log_name="deployer_command.log",
    )


if __name__ == "__main__":
    raise SystemExit(main())
