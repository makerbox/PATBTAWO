"""Built-in PATBTAWO context updater stage."""

from __future__ import annotations

from .stage_adapter import command_from_env, run_command_stage


def main() -> int:
    command = command_from_env(["PATBTAWO_CONTEXT_UPDATE_COMMAND", "PATBTAWO_CONTEXT_RUN_COMMAND"])
    return run_command_stage(
        stage="context",
        command=command,
        missing_status="blocked",
        missing_summary="Context updater is not configured.",
        missing_next_action="Set PATBTAWO_CONTEXT_UPDATE_COMMAND to a script or coding agent that refreshes context.md after successful tasks.",
        success_summary="Context file updated successfully.",
        failure_summary="Context update command failed.",
        log_name="context_command.log",
    )


if __name__ == "__main__":
    raise SystemExit(main())
