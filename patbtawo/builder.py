"""Built-in PATBTAWO builder stage."""

from __future__ import annotations

from .stage_adapter import command_from_env, run_command_stage


def main() -> int:
    command = command_from_env(["PATBTAWO_BUILDER_RUN_COMMAND", "PATBTAWO_BUILD_RUN_COMMAND"])
    return run_command_stage(
        stage="builder",
        command=command,
        missing_status="blocked",
        missing_summary="Builder is not configured.",
        missing_next_action="Set PATBTAWO_BUILDER_RUN_COMMAND to a coding agent or repository-specific build script.",
        success_summary="Builder command completed successfully.",
        failure_summary="Builder command failed.",
        log_name="builder_command.log",
    )


if __name__ == "__main__":
    raise SystemExit(main())
