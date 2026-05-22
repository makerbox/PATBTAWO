"""Built-in PATBTAWO planner stage."""

from __future__ import annotations

from .stage_adapter import command_from_env, run_command_stage


def main() -> int:
    command = command_from_env(["PATBTAWO_PLANNER_RUN_COMMAND", "PATBTAWO_PLAN_RUN_COMMAND"])
    return run_command_stage(
        stage="planner",
        command=command,
        missing_status="blocked",
        missing_summary="Planner is not configured.",
        missing_next_action="Set PATBTAWO_PLANNER_RUN_COMMAND to a planning agent or script that writes a task plan.",
        success_summary="Planner command completed successfully.",
        failure_summary="Planner command failed.",
        log_name="planner_command.log",
    )


if __name__ == "__main__":
    raise SystemExit(main())
