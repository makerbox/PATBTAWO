"""Built-in PATBTAWO verifier stage."""

from __future__ import annotations

from .stage_adapter import detect_verifier_command, run_command_stage


def main() -> int:
    command = detect_verifier_command()
    return run_command_stage(
        stage="verifier",
        command=command,
        missing_status="blocked",
        missing_summary="Verifier is not configured and no default Python verification command was detected.",
        missing_next_action="Set PATBTAWO_VERIFIER_RUN_COMMAND to an objective test/check command.",
        success_summary="Verifier command completed successfully.",
        failure_summary="Verifier command failed.",
        log_name="verifier_command.log",
    )


if __name__ == "__main__":
    raise SystemExit(main())
