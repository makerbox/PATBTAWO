#!/usr/bin/env python3
"""Provider-agnostic task workflow orchestrator.

The orchestrator intentionally keeps its dependencies to Python's standard
library so it can be dropped into small repositories without adding package
management first. Builder/verifier/context/deployer commands are supplied by
config and must write JSON reports to the path provided in
ORCHESTRATOR_REPORT_PATH.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Iterable as IterableABC
import dataclasses
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib import error, parse, request


ASANA_API_BASE = "https://app.asana.com/api/1.0"
TRELLO_API_BASE = "https://api.trello.com/1"
CLICKUP_API_BASE = "https://api.clickup.com/api/v2"
MONDAY_API_BASE = "https://api.monday.com/v2"
CLICKUP_TASK_ORDER_MODES = {"api", "orderindex"}
REQUIRED_REPORT_KEYS = (
    "task_id",
    "status",
    "summary",
    "changed_files",
    "checks",
    "failures",
    "next_action",
    "artifact_paths",
)
PLAN_TASK_REQUIRED_KEYS = ("title", "description")
SUCCESS_STATUSES = {"success", "succeeded", "pass", "passed", "ok", "done"}
FAILED_STATUSES = {"failed", "failure", "error", "errored"}
BLOCKED_STATUSES = {"blocked", "blocker", "external_blocker"}
TRANSIENT_EXIT_CODES = {75}
TASK_PAGE_LIMIT = 100
STAGE_COMMAND_PATH_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
SHELL_CONTROL_TOKENS = {"&&", "||", "|", ";", "(", ")"}
STAGE_ENV_PREFIXES = ("ORCHESTRATOR_STAGE_ENV_", "PATBTAWO_STAGE_ENV_")
STAGE_ENV_KEY_LIST = "ORCHESTRATOR_STAGE_ENV_KEYS"
DEPLOY_ENV_PREFIXES = ("ORCHESTRATOR_DEPLOY_", "ORCHESTRATOR_SMOKE_")
RESERVED_DEPLOY_ENV_KEYS = {
    "ORCHESTRATOR_DEPLOY_COMMAND",
    "ORCHESTRATOR_DEPLOY_ENABLED",
    "ORCHESTRATOR_DEPLOYER_AGENT_COMMAND",
    "ORCHESTRATOR_SMOKE_COMMAND",
}
DEPLOY_SKIP_PATTERN = re.compile(
    r"\[(?:skip|no)\s+(?:deploy|deployment|smoke)\]"
    r"|(?:^|\b)(?:skip|no)\s+(?:deploy|deployment|smoke)\b"
    r"|(?:^|\b)do\s+not\s+(?:deploy|smoke)\b"
    r"|(?:^|\b)(?:deploy|deployment|smoke)\s*:\s*(?:false|no|skip|none)\b",
    re.IGNORECASE,
)
DEPLOY_FORCE_PATTERN = re.compile(
    r"\[(?:deploy|deployment|smoke)\]"
    r"|(?:^|\b)(?:deploy|deployment|smoke)\s*:\s*(?:true|yes|required)\b",
    re.IGNORECASE,
)
PATBTAWO_RUN_COMMAND_KEYS = {
    "PATBTAWO_PLANNER_RUN_COMMAND",
    "PATBTAWO_PLAN_RUN_COMMAND",
    "PATBTAWO_BUILDER_RUN_COMMAND",
    "PATBTAWO_BUILD_RUN_COMMAND",
    "PATBTAWO_CONTEXT_UPDATE_COMMAND",
    "PATBTAWO_CONTEXT_RUN_COMMAND",
    "PATBTAWO_VERIFIER_RUN_COMMAND",
    "PATBTAWO_VERIFY_RUN_COMMAND",
    "PATBTAWO_DEPLOY_RUN_COMMAND",
    "PATBTAWO_SMOKE_RUN_COMMAND",
}
PLANNING_TASK_PATTERN = re.compile(
    r"\bbreak\b[\s\S]{0,160}\b(?:tasks?|tickets?|chunks?|work items?)\b"
    r"|\bcreate\s+(?:a\s+)?tasks?\s+for\s+each\s+chunk\b"
    r"|\bturn\b[\s\S]{0,160}\b(?:tasks?|tickets?|chunks?|work items?)\b",
    re.IGNORECASE,
)
PROCESS_ENCODING = "utf-8"
PROCESS_ERRORS = "replace"
ENV_REFERENCE_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
    r"|\$([A-Za-z_][A-Za-z0-9_]*)"
    r"|%([A-Za-z_][A-Za-z0-9_]*)%"
)


class ConfigError(RuntimeError):
    """Raised when required local configuration is missing or invalid."""


class MissingConfigError(ConfigError):
    """Raised when required environment configuration is missing."""


class CommandError(RuntimeError):
    """Raised when a local command cannot be completed."""

    def __init__(self, message: str, *, exit_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class ProviderError(RuntimeError):
    """Raised for task provider API failures."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class AsanaError(ProviderError):
    """Backward-compatible alias for older Asana-specific callers."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def compact_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def falsey(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "n", "off"}


def slug(value: str, *, fallback: str = "task") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned[:80] or fallback


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_env_file(path: Path, environ: Dict[str, str]) -> None:
    if not path.exists():
        raise ConfigError(f"Env file does not exist: {path}")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ConfigError(f"Invalid env file line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"Invalid env file line {line_number}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        environ.setdefault(key, value)


def run_local(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd),
        env=dict(env) if env else None,
        text=True,
        encoding=PROCESS_ENCODING,
        errors=PROCESS_ERRORS,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and completed.returncode != 0:
        output = completed.stdout.strip()
        raise CommandError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\n{output}",
            exit_code=completed.returncode,
        )
    return completed


def discover_repo_root(cwd: Path) -> Path:
    try:
        completed = run_local(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    except CommandError as exc:
        raise ConfigError(
            "This orchestrator must be launched from inside a git repository so "
            "it can create isolated worktrees."
        ) from exc
    root = completed.stdout.strip()
    if not root:
        raise ConfigError("git rev-parse returned an empty repository root")
    return Path(root).resolve()


def discover_base_branch(repo_root: Path) -> str:
    branch = run_local(["git", "branch", "--show-current"], cwd=repo_root, check=False).stdout.strip()
    return branch or "HEAD"


def ensure_under(path: Path, root: Path) -> None:
    path_resolved = path.resolve()
    root_resolved = root.resolve()
    if path_resolved == root_resolved:
        raise ConfigError(f"Refusing to manage root directory itself: {root_resolved}")
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ConfigError(f"Refusing to manage path outside {root_resolved}: {path_resolved}") from exc


def make_writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    except OSError:
        pass


def _windows_acl_reset(path: Path, *, recursive: bool = False) -> None:
    if os.name != "nt":
        return
    attrib_args = ["attrib", "-R", "-H", "-S", str(path)]
    if recursive:
        attrib_args.extend(["/S", "/D"])
    try:
        subprocess.run(
            attrib_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

    username = os.environ.get("USERNAME")
    if not username:
        return
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{username}" if domain else username
    icacls_args = ["icacls", str(path), "/grant", f"{principal}:(OI)(CI)F", "/C"]
    if recursive:
        icacls_args.extend(["/T"])
    try:
        subprocess.run(
            icacls_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def make_tree_removable(path: Path, *, reset_windows_acl: bool = False) -> None:
    if not path.exists():
        return
    if reset_windows_acl:
        _windows_acl_reset(path)
    make_writable(str(path))

    def onerror(exc: OSError) -> None:
        if exc.filename:
            make_writable(exc.filename)
            _windows_acl_reset(Path(exc.filename))

    for root, dirs, files in os.walk(path, topdown=False, onerror=onerror):
        for name in files:
            make_writable(str(Path(root) / name))
        for name in dirs:
            make_writable(str(Path(root) / name))


def rmtree_with_retries(path: Path, *, attempts: int = 5, delay_seconds: float = 0.25) -> Optional[Exception]:
    last_error: Optional[Exception] = None

    def onerror(func: Any, failed_path: str, _exc_info: Any) -> None:
        make_writable(failed_path)
        _windows_acl_reset(Path(failed_path))
        try:
            func(failed_path)
        except Exception as exc:  # pragma: no cover - exercised through shutil internals
            raise exc

    for attempt in range(1, attempts + 1):
        try:
            make_tree_removable(path, reset_windows_acl=attempt > 1)
            shutil.rmtree(path, onerror=onerror)
            return None
        except FileNotFoundError:
            return None
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(delay_seconds * attempt)
    return last_error


@dataclasses.dataclass(frozen=True)
class OrchestratorConfig:
    provider_name: str
    provider_options: Dict[str, str]
    project_gid: str
    sections: Dict[str, str]
    retry_limit: int
    dry_run: bool
    base_branch: str
    repo_root: Path
    context_path: Path
    worktree_root: Path
    artifact_root: Path
    builder_command: str
    verifier_command: str
    context_update_command: Optional[str]
    planner_command: Optional[str]
    planner_tag: Optional[str]
    deploy_command: Optional[str]
    smoke_command: Optional[str]
    deploy_enabled: bool
    objective_verifier: bool
    keep_worktrees: bool
    stage_timeout_seconds: Optional[int]
    stage_environment: Dict[str, str]
    deploy_policy: str = "all"
    deploy_tag: Optional[str] = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        cwd: Path,
        dry_run_override: bool = False,
        provider_override: Optional[str] = None,
    ) -> "OrchestratorConfig":
        repo_root = discover_repo_root(cwd)
        provider_name = normalize_provider_name(provider_override or environ.get("ORCHESTRATOR_PROVIDER"))
        provider_options = provider_options_from_env(provider_name, environ)
        project_gid = provider_project_id(provider_name, environ, provider_options)
        sections = lifecycle_states_from_env(provider_name, environ)
        builder_command = first_env(
            environ,
            ["ORCHESTRATOR_BUILDER_AGENT_COMMAND", "ORCHESTRATOR_BUILDER_COMMAND"],
            required=True,
        )
        verifier_command = first_env(
            environ,
            ["ORCHESTRATOR_VERIFIER_AGENT_COMMAND", "ORCHESTRATOR_VERIFIER_COMMAND"],
            required=True,
        )

        retry_limit = parse_nonnegative_int(environ.get("ORCHESTRATOR_RETRY_LIMIT"), default=0)
        stage_timeout = parse_optional_positive_int(environ.get("ORCHESTRATOR_STAGE_TIMEOUT_SECONDS"))

        worktree_root = Path(
            environ.get("ORCHESTRATOR_WORKTREE_ROOT")
            or repo_root.parent / f".{repo_root.name}-orchestrator-worktrees"
        ).expanduser()
        context_path = Path(environ.get("ORCHESTRATOR_CONTEXT_PATH") or repo_root / "context.md").expanduser()
        artifact_root = Path(
            environ.get("ORCHESTRATOR_ARTIFACT_ROOT") or repo_root / ".orchestrator" / "artifacts"
        ).expanduser()
        if not worktree_root.is_absolute():
            worktree_root = (repo_root / worktree_root).resolve()
        if not context_path.is_absolute():
            context_path = (repo_root / context_path).resolve()
        if not artifact_root.is_absolute():
            artifact_root = (repo_root / artifact_root).resolve()

        context_update_command = blank_to_none(
            first_env(environ, ["ORCHESTRATOR_CONTEXT_UPDATER_AGENT_COMMAND", "ORCHESTRATOR_CONTEXT_UPDATE_COMMAND"])
        )
        planner_command = blank_to_none(
            first_env(environ, ["ORCHESTRATOR_PLANNER_AGENT_COMMAND", "ORCHESTRATOR_PLANNER_COMMAND"])
        )
        planner_tag = blank_to_none(environ.get("ORCHESTRATOR_PLANNER_TAG") or "plan")
        deploy_command = blank_to_none(
            first_env(environ, ["ORCHESTRATOR_DEPLOYER_AGENT_COMMAND", "ORCHESTRATOR_DEPLOY_COMMAND"])
        )
        smoke_command = blank_to_none(environ.get("ORCHESTRATOR_SMOKE_COMMAND"))
        deploy_policy = normalize_deploy_policy(
            environ.get("ORCHESTRATOR_DEPLOY_POLICY"),
            deploy_enabled_value=environ.get("ORCHESTRATOR_DEPLOY_ENABLED"),
            has_deploy_command=bool(deploy_command or smoke_command),
        )
        deploy_tag = blank_to_none(environ.get("ORCHESTRATOR_DEPLOY_TAG"))
        deploy_enabled = deploy_policy != "none"
        if deploy_policy == "tagged" and not deploy_tag:
            raise ConfigError("ORCHESTRATOR_DEPLOY_POLICY=tagged requires ORCHESTRATOR_DEPLOY_TAG")
        if deploy_enabled and not (deploy_command or smoke_command):
            raise ConfigError(
                "Deployment is enabled but neither "
                "ORCHESTRATOR_DEPLOY_COMMAND nor ORCHESTRATOR_SMOKE_COMMAND is set"
            )

        return cls(
            provider_name=provider_name,
            provider_options=provider_options,
            project_gid=project_gid,
            sections=sections,
            retry_limit=retry_limit,
            dry_run=dry_run_override or truthy(environ.get("ORCHESTRATOR_DRY_RUN")),
            base_branch=environ.get("ORCHESTRATOR_BASE_BRANCH") or discover_base_branch(repo_root),
            repo_root=repo_root,
            context_path=context_path.resolve(),
            worktree_root=worktree_root.resolve(),
            artifact_root=artifact_root.resolve(),
            builder_command=builder_command,
            verifier_command=verifier_command,
            context_update_command=context_update_command,
            planner_command=planner_command,
            planner_tag=planner_tag,
            deploy_command=deploy_command,
            smoke_command=smoke_command,
            deploy_enabled=deploy_enabled,
            objective_verifier=not falsey(environ.get("ORCHESTRATOR_OBJECTIVE_VERIFIER")),
            keep_worktrees=truthy(environ.get("ORCHESTRATOR_KEEP_WORKTREES")),
            stage_timeout_seconds=stage_timeout,
            stage_environment=stage_environment_from_env(environ),
            deploy_policy=deploy_policy,
            deploy_tag=deploy_tag,
        )

    def deploy_stage_command(self) -> Optional[str]:
        commands = [cmd for cmd in (self.deploy_command, self.smoke_command) if cmd]
        if not commands:
            return None
        if len(commands) == 1:
            return commands[0]
        return " && ".join(commands)

    def redacted_dict(self) -> Dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["provider_options"] = redact_secrets(self.provider_options)
        payload["stage_environment"] = redact_secrets(self.stage_environment)
        for key in ("repo_root", "context_path", "worktree_root", "artifact_root"):
            payload[key] = str(payload[key])
        return payload


@dataclasses.dataclass(frozen=True)
class AttemptResetConfig:
    repo_root: Path
    worktree_root: Path
    artifact_root: Path


def attempt_reset_config_from_env(environ: Mapping[str, str], *, cwd: Path) -> AttemptResetConfig:
    repo_root = discover_repo_root(cwd)
    worktree_root = Path(
        environ.get("ORCHESTRATOR_WORKTREE_ROOT")
        or repo_root.parent / f".{repo_root.name}-orchestrator-worktrees"
    ).expanduser()
    artifact_root = Path(
        environ.get("ORCHESTRATOR_ARTIFACT_ROOT") or repo_root / ".orchestrator" / "artifacts"
    ).expanduser()

    if not worktree_root.is_absolute():
        worktree_root = (repo_root / worktree_root).resolve()
    if not artifact_root.is_absolute():
        artifact_root = (repo_root / artifact_root).resolve()

    return AttemptResetConfig(
        repo_root=repo_root,
        worktree_root=worktree_root.resolve(),
        artifact_root=artifact_root.resolve(),
    )


def parse_nonnegative_int(value: Optional[str], *, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"Expected a non-negative integer, got: {value}") from exc
    if parsed < 0:
        raise ConfigError(f"Expected a non-negative integer, got: {value}")
    return parsed


def normalize_deploy_policy(
    value: Optional[str],
    *,
    deploy_enabled_value: Optional[str],
    has_deploy_command: bool,
) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        if deploy_enabled_value is not None:
            if falsey(deploy_enabled_value):
                return "none"
            if truthy(deploy_enabled_value):
                return "all"
            raise ConfigError(
                "ORCHESTRATOR_DEPLOY_ENABLED must be true/false when ORCHESTRATOR_DEPLOY_POLICY is not set"
            )
        return "all" if has_deploy_command else "none"

    aliases = {
        "all": "all",
        "always": "all",
        "every": "all",
        "every-task": "all",
        "every_task": "all",
        "tag": "tagged",
        "tags": "tagged",
        "tagged": "tagged",
        "tagged-only": "tagged",
        "tagged_only": "tagged",
        "none": "none",
        "never": "none",
        "off": "none",
        "false": "none",
        "disabled": "none",
        "disable": "none",
    }
    if raw not in aliases:
        raise ConfigError(
            "ORCHESTRATOR_DEPLOY_POLICY must be one of: all, tagged, none"
        )
    return aliases[raw]


def parse_optional_positive_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"Expected a positive integer, got: {value}") from exc
    if parsed <= 0:
        raise ConfigError(f"Expected a positive integer, got: {value}")
    return parsed


def blank_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize_provider_name(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if not normalized:
        return "asana"
    aliases = {
        "monday": "monday",
        "monday.com": "monday",
        "mondaycom": "monday",
        "click-up": "clickup",
        "clickup": "clickup",
        "command-provider": "command",
        "custom": "command",
        "generic": "command",
    }
    return aliases.get(normalized, normalized)


def required_env(environ: Mapping[str, str], names: Sequence[str]) -> Dict[str, str]:
    missing = [name for name in names if not environ.get(name)]
    if missing:
        raise MissingConfigError("Missing required configuration: " + ", ".join(missing))
    return {name: environ[name] for name in names}


def first_env(environ: Mapping[str, str], names: Sequence[str], *, required: bool = False) -> str:
    for name in names:
        value = environ.get(name)
        if value:
            return value
    if required:
        raise MissingConfigError("Missing required configuration: " + " or ".join(names))
    return ""


def split_env_key_list(value: str) -> List[str]:
    keys: List[str] = []
    for part in re.split(r"[,\s]+", value):
        key = part.strip()
        if key:
            keys.append(key)
    return keys


def stage_environment_from_env(environ: Mapping[str, str]) -> Dict[str, str]:
    stage_env: Dict[str, str] = {}

    for key in split_env_key_list(environ.get(STAGE_ENV_KEY_LIST, "")):
        if key in environ:
            stage_env[key] = environ[key]

    for key, value in environ.items():
        if key == STAGE_ENV_KEY_LIST:
            continue
        if key == "ORCHESTRATOR_MODEL":
            stage_env[key] = value
            continue
        for prefix in STAGE_ENV_PREFIXES:
            if key.startswith(prefix):
                target = key.removeprefix(prefix)
                if target:
                    stage_env[target] = value
                break
        else:
            if key.startswith(DEPLOY_ENV_PREFIXES) and key not in RESERVED_DEPLOY_ENV_KEYS:
                stage_env[key] = value
            elif key in PATBTAWO_RUN_COMMAND_KEYS:
                stage_env[key] = value

    return stage_env


def expand_command_env(command: str, environ: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or match.group(3) or ""
        return str(environ.get(name, match.group(0)))

    return ENV_REFERENCE_PATTERN.sub(replace, command)


def command_tokens(command: str) -> List[str]:
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return []
    return [token.strip("\"'") for token in tokens if token.strip("\"'")]


def is_local_path_token(token: str) -> bool:
    stripped = token.strip().strip("\"'")
    if not stripped or stripped.startswith("-"):
        return False
    if stripped in SHELL_CONTROL_TOKENS or "://" in stripped:
        return False
    if "$" in stripped or "%" in stripped:
        return False
    if any(char in stripped for char in "*?<>"):
        return False
    normalized = stripped.replace("\\", "/")
    suffix = Path(normalized).suffix.lower()
    return (
        suffix in STAGE_COMMAND_PATH_EXTENSIONS
        or normalized.startswith("./")
        or normalized.startswith("../")
        or "/" in normalized
    )


def referenced_local_paths(command: str, repo_root: Path) -> List[Tuple[str, Path]]:
    paths: List[Tuple[str, Path]] = []
    seen: set[str] = set()
    expanded_command = expand_command_env(command, os.environ)
    for token in command_tokens(expanded_command):
        if not is_local_path_token(token):
            continue
        raw_path = Path(os.path.expanduser(os.path.expandvars(token)))
        resolved = raw_path if raw_path.is_absolute() else repo_root / raw_path
        key = str(resolved)
        if key not in seen:
            paths.append((token, resolved))
            seen.add(key)
    return paths


def validate_runtime_config(config: "OrchestratorConfig") -> None:
    checks: List[Tuple[str, Optional[str]]] = [
        ("builder", config.builder_command),
        ("verifier", config.verifier_command),
    ]
    if config.context_update_command:
        checks.append(("context", config.context_update_command))
    if config.planner_command:
        checks.append(("planner", config.planner_command))
    if config.deploy_enabled:
        checks.extend(
            [
                ("deployer", config.deploy_command),
                ("smoke", config.smoke_command),
            ]
        )

    errors: List[str] = []
    if config.context_update_command or config.planner_command:
        try:
            ensure_under(config.context_path, config.repo_root)
        except ConfigError as exc:
            errors.append(str(exc))
        if not config.context_path.exists():
            errors.append(f"Missing shared context file: {config.context_path}")

    for stage, command in checks:
        if not command:
            continue
        missing = [(token, path) for token, path in referenced_local_paths(command, config.repo_root) if not path.exists()]
        for token, path in missing:
            errors.append(
                f"{stage} command references missing local path '{token}' "
                f"(resolved to {path}). Set the stage command to a real agent/script "
                "that writes JSON to ORCHESTRATOR_REPORT_PATH."
            )
        if codex_approval_flag_after_exec(command):
            errors.append(
                f"{stage} command places --ask-for-approval after 'codex exec'. "
                "Move it before the subcommand, for example: "
                "codex --ask-for-approval never exec --sandbox workspace-write ..."
            )

    for name, command in sorted(config.stage_environment.items()):
        if name not in PATBTAWO_RUN_COMMAND_KEYS or not command:
            continue
        if codex_approval_flag_after_exec(command):
            errors.append(
                f"{name} places --ask-for-approval after 'codex exec'. "
                "Move it before the subcommand, for example: "
                "codex --ask-for-approval never exec --sandbox workspace-write ..."
            )

    if errors:
        raise ConfigError("Stage command configuration is invalid:\n- " + "\n- ".join(errors))


def codex_approval_flag_after_exec(command: str) -> bool:
    lowered = command.lower()
    ask_index = lowered.find("--ask-for-approval")
    if ask_index < 0:
        return False
    exec_index = lowered.find(" exec ")
    return exec_index >= 0 and ask_index > exec_index and "codex" in lowered[:exec_index]


def output_tail(output: str, *, max_lines: int = 5, max_chars: int = 500) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    tail = " / ".join(lines[-max_lines:])
    if len(tail) > max_chars:
        return "..." + tail[-max_chars:]
    return tail


def sortable_scalar(value: Any) -> Tuple[int, Any]:
    if value is None:
        return (2, "")
    if isinstance(value, bool):
        return (0, Decimal(int(value)))
    if isinstance(value, (int, float, Decimal)):
        parsed = Decimal(str(value))
        if not parsed.is_finite():
            return (1, str(value).lower())
        return (0, parsed)
    text = str(value).strip()
    if not text:
        return (2, "")
    try:
        parsed = Decimal(text)
        if not parsed.is_finite():
            return (1, text.lower())
        return (0, parsed)
    except InvalidOperation:
        return (1, text.lower())


def first_present_value(item: Mapping[str, Any], fields: Sequence[str]) -> Any:
    for field in fields:
        value = item.get(field)
        if value not in (None, ""):
            return value
    return None


def top_down_task(tasks: Sequence[Mapping[str, Any]], *, position_fields: Sequence[str] = ()) -> Optional[Mapping[str, Any]]:
    if not tasks:
        return None
    if not position_fields:
        return tasks[0]

    indexed = list(enumerate(tasks))
    if not any(first_present_value(task, position_fields) not in (None, "") for _, task in indexed):
        return tasks[0]

    def key(pair: Tuple[int, Mapping[str, Any]]) -> Tuple[int, Tuple[int, Any], int]:
        index, task = pair
        value = first_present_value(task, position_fields)
        if value in (None, ""):
            return (1, sortable_scalar(None), index)
        return (0, sortable_scalar(value), index)

    return sorted(indexed, key=key)[0][1]


def jql_has_order_by(jql: str) -> bool:
    return bool(re.search(r"\border\s+by\b", jql, flags=re.IGNORECASE))


def jql_with_top_down_order(jql: str) -> str:
    stripped = jql.strip()
    if jql_has_order_by(stripped):
        return stripped
    return f"{stripped} ORDER BY Rank ASC"


def lifecycle_states_from_env(provider_name: str, environ: Mapping[str, str]) -> Dict[str, str]:
    states: Dict[str, str] = {}
    for state in ("ready", "building", "verifying", "failed", "deploying", "done", "blocked"):
        states[state] = first_env(
            environ,
            lifecycle_state_env_names(provider_name, state),
            required=True,
        )
    return states


def lifecycle_state_env_names(provider_name: str, state: str) -> List[str]:
    upper = state.upper()
    names = [
        f"ORCHESTRATOR_STATE_{upper}_ID",
        f"ORCHESTRATOR_STATE_{upper}",
    ]
    if provider_name == "asana":
        names.append(f"ASANA_SECTION_{upper}_GID")
    elif provider_name == "trello":
        names.append(f"TRELLO_LIST_{upper}_ID")
    elif provider_name == "clickup":
        names.append(f"CLICKUP_STATUS_{upper}")
    elif provider_name == "jira":
        names.extend([f"JIRA_TRANSITION_{upper}_ID", f"JIRA_STATUS_{upper}"])
    elif provider_name == "monday":
        names.append(f"MONDAY_STATUS_{upper}")
    return names


def provider_options_from_env(provider_name: str, environ: Mapping[str, str]) -> Dict[str, str]:
    if provider_name == "asana":
        return required_env(environ, ["ASANA_ACCESS_TOKEN"])
    if provider_name == "trello":
        return required_env(environ, ["TRELLO_API_KEY", "TRELLO_TOKEN"])
    if provider_name == "clickup":
        options = required_env(environ, ["CLICKUP_ACCESS_TOKEN", "CLICKUP_LIST_ID"])
        options["CLICKUP_API_BASE"] = environ.get("CLICKUP_API_BASE", CLICKUP_API_BASE)
        options["CLICKUP_TASK_ORDER"] = (environ.get("CLICKUP_TASK_ORDER") or "api").strip().lower() or "api"
        options["CLICKUP_TASK_REVERSE"] = environ.get("CLICKUP_TASK_REVERSE", "true")
        if environ.get("CLICKUP_VIEW_ID"):
            options["CLICKUP_VIEW_ID"] = environ["CLICKUP_VIEW_ID"]
        if options["CLICKUP_TASK_ORDER"] not in CLICKUP_TASK_ORDER_MODES:
            raise ConfigError(
                "CLICKUP_TASK_ORDER must be one of: "
                + ", ".join(sorted(CLICKUP_TASK_ORDER_MODES))
            )
        return options
    if provider_name == "jira":
        options = required_env(environ, ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
        if not environ.get("JIRA_READY_JQL") and not environ.get("JIRA_PROJECT_KEY"):
            raise ConfigError("Missing required configuration: JIRA_READY_JQL or JIRA_PROJECT_KEY")
        for name in ("JIRA_PROJECT_KEY", "JIRA_READY_JQL", "JIRA_SEARCH_ENDPOINT"):
            if environ.get(name):
                options[name] = environ[name]
        return options
    if provider_name == "monday":
        options = required_env(environ, ["MONDAY_API_TOKEN", "MONDAY_BOARD_ID", "MONDAY_STATUS_COLUMN_ID"])
        if environ.get("MONDAY_API_VERSION"):
            options["MONDAY_API_VERSION"] = environ["MONDAY_API_VERSION"]
        return options
    if provider_name == "command":
        options = required_env(
            environ,
            [
                "ORCHESTRATOR_COMMAND_NEXT_TASK",
                "ORCHESTRATOR_COMMAND_GET_TASK",
                "ORCHESTRATOR_COMMAND_MOVE_TASK",
                "ORCHESTRATOR_COMMAND_COMMENT_TASK",
            ],
        )
        if environ.get("ORCHESTRATOR_COMMAND_CREATE_TASK"):
            options["ORCHESTRATOR_COMMAND_CREATE_TASK"] = environ["ORCHESTRATOR_COMMAND_CREATE_TASK"]
        return options
    raise ConfigError(
        f"Unsupported ORCHESTRATOR_PROVIDER '{provider_name}'. "
        "Supported providers: asana, trello, clickup, jira, monday, command."
    )


def provider_project_id(
    provider_name: str,
    environ: Mapping[str, str],
    provider_options: Mapping[str, str],
) -> str:
    generic = environ.get("ORCHESTRATOR_PROJECT_ID") or environ.get("ORCHESTRATOR_PROJECT_GID")
    if generic:
        return generic
    if provider_name == "asana":
        return first_env(environ, ["ASANA_PROJECT_GID"], required=True)
    if provider_name == "trello":
        return environ.get("TRELLO_BOARD_ID", "")
    if provider_name == "clickup":
        return provider_options["CLICKUP_LIST_ID"]
    if provider_name == "jira":
        return provider_options.get("JIRA_PROJECT_KEY", "")
    if provider_name == "monday":
        return provider_options["MONDAY_BOARD_ID"]
    return environ.get("ORCHESTRATOR_PROJECT_ID", "")


def redact_secrets(payload: Mapping[str, str]) -> Dict[str, str]:
    redacted: Dict[str, str] = {}
    secret_markers = ("TOKEN", "KEY", "SECRET", "PASSWORD", "EMAIL")
    for key, value in payload.items():
        redacted[key] = "***" if any(marker in key.upper() for marker in secret_markers) else value
    return redacted


def configuration_help(provider_name: str, error: Exception) -> str:
    lines = [
        str(error),
        "",
        f"Selected provider: {provider_name}",
    ]
    if provider_name == "asana":
        lines.extend(
            [
                "Asana requires ASANA_ACCESS_TOKEN, ASANA_PROJECT_GID, seven section IDs, and builder/verifier commands.",
                "Set ORCHESTRATOR_PROVIDER=asana or pass --provider asana explicitly.",
            ]
        )
    elif provider_name == "trello":
        lines.append("Trello requires TRELLO_API_KEY, TRELLO_TOKEN, list IDs, and builder/verifier commands.")
    elif provider_name == "clickup":
        lines.append("ClickUp requires CLICKUP_ACCESS_TOKEN, CLICKUP_LIST_ID, status names or status IDs, and builder/verifier commands.")
    elif provider_name == "jira":
        lines.append("Jira requires JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, Ready query/project config, transitions/statuses, and builder/verifier commands.")
    elif provider_name == "monday":
        lines.append("monday.com requires MONDAY_API_TOKEN, MONDAY_BOARD_ID, MONDAY_STATUS_COLUMN_ID, status labels, and builder/verifier commands.")
    elif provider_name == "command":
        lines.append("The command provider requires the four ORCHESTRATOR_COMMAND_* hooks, lifecycle states, and builder/verifier commands.")
    lines.extend(
        [
            "Copy .env.example to .env, fill in the values for your provider, then rerun:",
            "  python -m patbtawo --validate-config --print-config",
            "Use --provider trello|clickup|jira|monday|asana|command if you do not want the default Asana provider.",
        ]
    )
    return "\n".join(lines)


class AsanaClient:
    def __init__(
        self,
        token: str,
        *,
        dry_run: bool = False,
        api_base: str = ASANA_API_BASE,
        project_gid: str = "",
    ) -> None:
        self.token = token
        self.dry_run = dry_run
        self.api_base = api_base.rstrip("/")
        self.project_gid = project_gid

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        response = self._request(
            "GET",
            f"/sections/{section_gid}/tasks",
            params={
                "limit": str(TASK_PAGE_LIMIT),
                "opt_fields": "gid,name,resource_type,modified_at,permalink_url",
            },
        )
        tasks = response.get("data") or []
        task = top_down_task(tasks)
        return dict(task) if task else None

    def get_task_contract(self, task_gid: str) -> Dict[str, Any]:
        response = self._request(
            "GET",
            f"/tasks/{task_gid}",
            params={
                "opt_fields": ",".join(
                    [
                        "gid",
                        "name",
                        "notes",
                        "html_notes",
                        "permalink_url",
                        "created_at",
                        "modified_at",
                        "completed",
                        "resource_subtype",
                        "custom_fields",
                        "custom_fields.name",
                        "custom_fields.display_value",
                        "memberships",
                        "memberships.project",
                        "memberships.project.name",
                        "memberships.section",
                        "memberships.section.name",
                        "projects",
                        "projects.name",
                        "tags",
                        "tags.name",
                    ]
                )
            },
        )
        return response["data"]

    def move_task(self, task_gid: str, section_gid: str) -> None:
        self._request(
            "POST",
            f"/sections/{section_gid}/addTask",
            data={"task": task_gid},
            mutation=True,
        )

    def comment(self, task_gid: str, text: str) -> None:
        self._request(
            "POST",
            f"/tasks/{task_gid}/stories",
            data={"text": text},
            mutation=True,
        )

    def create_task(
        self,
        *,
        title: str,
        description: str,
        ready_state: str,
        tags: Sequence[str] = (),
        parent_task_id: str = "",
    ) -> Dict[str, Any]:
        if not self.project_gid:
            raise ProviderError("Asana task creation requires ASANA_PROJECT_GID or ORCHESTRATOR_PROJECT_ID")
        data: Dict[str, Any] = {
            "name": title,
            "notes": description,
            "projects": [self.project_gid],
        }
        if parent_task_id:
            data["parent"] = parent_task_id
        if self.dry_run:
            print(f"[dry-run] Asana create task in project {self.project_gid}: {title}")
            return normalized_task(provider="asana", task_id=f"dry-run-{slug(title)}", name=title, notes=description)
        response = self._request("POST", "/tasks", data=data, mutation=True)
        task = response.get("data") or {}
        task_id = str(task.get("gid") or task.get("id") or "")
        if not task_id:
            raise ProviderError("Asana create task response did not include a task gid")
        self.move_task(task_id, ready_state)
        if tags:
            self.comment(task_id, "Tags requested by planner: " + ", ".join(tags))
        return normalized_task(
            provider="asana",
            task_id=task_id,
            name=task.get("name") or title,
            notes=description,
            url=task.get("permalink_url") or "",
            raw=task,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        data: Optional[Mapping[str, Any]] = None,
        mutation: bool = False,
    ) -> Dict[str, Any]:
        if self.dry_run and mutation:
            print(f"[dry-run] Asana {method} {path} {json.dumps(data or {}, sort_keys=True)}")
            return {"data": {}}

        query = ""
        if params:
            query = "?" + parse.urlencode(params)
        url = self.api_base + path + query
        body = None if data is None else json.dumps({"data": data}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        req = request.Request(url, data=body, headers=headers, method=method)

        for attempt in range(4):
            try:
                with request.urlopen(req, timeout=30) as response:
                    content = response.read().decode("utf-8")
                    return json.loads(content) if content else {"data": {}}
            except error.HTTPError as exc:
                retryable = exc.code in {429, 500, 502, 503, 504}
                message = self._format_http_error(exc)
                if retryable and attempt < 3:
                    retry_after = exc.headers.get("Retry-After")
                    sleep_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    time.sleep(min(sleep_seconds, 30))
                    continue
                raise AsanaError(message, retryable=retryable) from exc
            except error.URLError as exc:
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise AsanaError(f"Asana request failed: {exc}", retryable=True) from exc
        raise AsanaError("Asana request failed after retries", retryable=True)

    @staticmethod
    def _format_http_error(exc: error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        return f"Asana API returned HTTP {exc.code}: {body or exc.reason}"


class HttpJsonClient:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        form_body: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        query = ""
        if params:
            query = "?" + parse.urlencode(params, doseq=True)
        body = None
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form_body is not None:
            body = parse.urlencode(form_body, doseq=True).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = request.Request(url + query, data=body, headers=request_headers, method=method)
        for attempt in range(4):
            try:
                with request.urlopen(req, timeout=30) as response:
                    content = response.read().decode("utf-8")
                    return json.loads(content) if content else {}
            except error.HTTPError as exc:
                retryable = exc.code in {429, 500, 502, 503, 504}
                message = self._format_http_error(exc)
                if retryable and attempt < 3:
                    retry_after = exc.headers.get("Retry-After")
                    sleep_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    time.sleep(min(sleep_seconds, 30))
                    continue
                raise ProviderError(message, retryable=retryable) from exc
            except error.URLError as exc:
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise ProviderError(f"Provider request failed: {exc}", retryable=True) from exc
        raise ProviderError("Provider request failed after retries", retryable=True)

    @staticmethod
    def _format_http_error(exc: error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        return f"Provider API returned HTTP {exc.code}: {body or exc.reason}"


def normalized_task(
    *,
    provider: str,
    task_id: Any,
    name: Any,
    notes: Any = "",
    url: Any = "",
    raw: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "gid": str(task_id),
        "name": str(name or ""),
        "notes": str(notes or ""),
        "permalink_url": str(url or ""),
        "provider": provider,
        "raw": dict(raw or {}),
    }


class TrelloProvider:
    def __init__(self, api_key: str, token: str, *, dry_run: bool = False) -> None:
        self.api_key = api_key
        self.token = token
        self.dry_run = dry_run
        self.http = HttpJsonClient()

    def _params(self, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        params = {"key": self.api_key, "token": self.token}
        if extra:
            params.update(extra)
        return params

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        cards = self.http.request(
            "GET",
            f"{TRELLO_API_BASE}/lists/{section_gid}/cards",
            params=self._params({"limit": TASK_PAGE_LIMIT, "fields": "id,name,desc,url,idList,dateLastActivity,pos"}),
        )
        if not cards:
            return None
        card = top_down_task(cards, position_fields=("pos",)) or cards[0]
        return normalized_task(
            provider="trello",
            task_id=card["id"],
            name=card.get("name"),
            notes=card.get("desc"),
            url=card.get("url"),
            raw=card,
        )

    def get_task_contract(self, task_gid: str) -> Dict[str, Any]:
        card = self.http.request(
            "GET",
            f"{TRELLO_API_BASE}/cards/{task_gid}",
            params=self._params({"fields": "all", "members": "false", "checklists": "all"}),
        )
        return normalized_task(
            provider="trello",
            task_id=card["id"],
            name=card.get("name"),
            notes=card.get("desc"),
            url=card.get("url"),
            raw=card,
        )

    def move_task(self, task_gid: str, section_gid: str) -> None:
        if self.dry_run:
            print(f"[dry-run] Trello move card {task_gid} to list {section_gid}")
            return
        self.http.request(
            "PUT",
            f"{TRELLO_API_BASE}/cards/{task_gid}",
            params=self._params({"idList": section_gid}),
        )

    def comment(self, task_gid: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] Trello comment on card {task_gid}: {text}")
            return
        self.http.request(
            "POST",
            f"{TRELLO_API_BASE}/cards/{task_gid}/actions/comments",
            params=self._params({"text": text}),
        )

    def create_task(
        self,
        *,
        title: str,
        description: str,
        ready_state: str,
        tags: Sequence[str] = (),
        parent_task_id: str = "",
    ) -> Dict[str, Any]:
        desc = description
        if tags:
            desc += "\n\nTags: " + ", ".join(tags)
        if parent_task_id:
            desc += f"\n\nPlanned from: {parent_task_id}"
        if self.dry_run:
            print(f"[dry-run] Trello create card in list {ready_state}: {title}")
            return normalized_task(provider="trello", task_id=f"dry-run-{slug(title)}", name=title, notes=desc)
        card = self.http.request(
            "POST",
            f"{TRELLO_API_BASE}/cards",
            params=self._params({"idList": ready_state, "name": title, "desc": desc}),
        )
        return normalized_task(
            provider="trello",
            task_id=card["id"],
            name=card.get("name") or title,
            notes=card.get("desc") or desc,
            url=card.get("url") or "",
            raw=card,
        )


class ClickUpProvider:
    def __init__(
        self,
        access_token: str,
        list_id: str,
        *,
        dry_run: bool = False,
        api_base: str = CLICKUP_API_BASE,
        view_id: str = "",
        task_order: str = "api",
        task_reverse: str = "true",
    ) -> None:
        self.access_token = access_token
        self.list_id = list_id
        self.dry_run = dry_run
        self.api_base = api_base.rstrip("/")
        self.http = HttpJsonClient()
        self._status_aliases: Optional[Dict[str, str]] = None
        self.view_id = view_id.strip()
        self.task_order = task_order.strip().lower() or "api"
        self.task_reverse = "false" if falsey(task_reverse) else "true"

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": self.access_token}

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        tasks = self._ready_tasks(section_gid)
        position_fields = ()
        if not self.view_id and self.task_order == "orderindex":
            position_fields = ("orderindex", "order_index", "pos", "position")
        task = top_down_task(tasks, position_fields=position_fields)
        return self._normalize(task) if task else None

    def _ready_tasks(self, section_gid: str) -> List[Mapping[str, Any]]:
        ready_values = self._status_match_values(section_gid)
        if self.view_id:
            return self._ready_tasks_from_view(ready_values)
        return self._ready_tasks_from_list(section_gid, ready_values)

    def _ready_tasks_from_list(self, section_gid: str, ready_values: set[str]) -> List[Mapping[str, Any]]:
        status_filter = self._status_api_value(section_gid)
        if self._looks_like_status_id(section_gid) and status_filter.strip().lower() == section_gid.strip().lower():
            status_filter = ""
        matches: List[Mapping[str, Any]] = []
        for page in range(20):
            params: Dict[str, Any] = {
                "archived": "false",
                "include_closed": "true",
                "subtasks": "true",
                "page": page,
                "reverse": self.task_reverse,
            }
            if status_filter:
                params["statuses[]"] = [status_filter]
            response = self.http.request(
                "GET",
                f"{self.api_base}/list/{self.list_id}/task",
                headers=self.headers,
                params=params,
            )
            tasks = response.get("tasks") or []
            for task in tasks:
                if self._task_matches_status(task, ready_values):
                    matches.append(task)
            if not tasks or response.get("last_page"):
                break
        return matches

    def _ready_tasks_from_view(self, ready_values: set[str]) -> List[Mapping[str, Any]]:
        matches: List[Mapping[str, Any]] = []
        for page in range(20):
            response = self.http.request(
                "GET",
                f"{self.api_base}/view/{self.view_id}/task",
                headers=self.headers,
                params={"page": page},
            )
            tasks = response.get("tasks") or []
            for task in tasks:
                if self._task_matches_status(task, ready_values):
                    matches.append(task)
            if not tasks or response.get("last_page"):
                break
        return matches

    def get_task_contract(self, task_gid: str) -> Dict[str, Any]:
        return self._normalize(
            self.http.request(
                "GET",
                f"{self.api_base}/task/{task_gid}",
                headers=self.headers,
            )
        )

    def move_task(self, task_gid: str, section_gid: str) -> None:
        status_value = self._status_api_value(section_gid) or section_gid
        if self.dry_run:
            print(f"[dry-run] ClickUp move task {task_gid} to status {status_value}")
            return
        self.http.request(
            "PUT",
            f"{self.api_base}/task/{task_gid}",
            headers=self.headers,
            json_body={"status": status_value},
        )

    def comment(self, task_gid: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] ClickUp comment on task {task_gid}: {text}")
            return
        self.http.request(
            "POST",
            f"{self.api_base}/task/{task_gid}/comment",
            headers=self.headers,
            json_body={"comment_text": text},
        )

    def create_task(
        self,
        *,
        title: str,
        description: str,
        ready_state: str,
        tags: Sequence[str] = (),
        parent_task_id: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "name": title,
            "description": description,
            "status": self._status_api_value(ready_state) or ready_state,
        }
        if tags:
            body["tags"] = list(tags)
        if parent_task_id:
            body["parent"] = parent_task_id
        if self.dry_run:
            print(f"[dry-run] ClickUp create task in list {self.list_id}: {title}")
            return normalized_task(provider="clickup", task_id=f"dry-run-{slug(title)}", name=title, notes=description)
        task = self.http.request(
            "POST",
            f"{self.api_base}/list/{self.list_id}/task",
            headers=self.headers,
            json_body=body,
        )
        return self._normalize(task)

    def _status_api_value(self, configured_status: str) -> str:
        configured = configured_status.strip()
        aliases = self._load_status_aliases()
        return aliases.get(configured.lower(), configured)

    @staticmethod
    def _looks_like_status_id(value: str) -> bool:
        return bool(re.match(r"^p\d+_", value.strip(), flags=re.IGNORECASE))

    def _status_match_values(self, configured_status: str) -> set[str]:
        configured = configured_status.strip()
        values = {configured.lower()}
        resolved = self._status_api_value(configured)
        if resolved:
            values.add(resolved.lower())
        aliases = self._load_status_aliases()
        for key, value in aliases.items():
            if key == configured.lower() or value.lower() == configured.lower():
                values.add(key)
                values.add(value.lower())
        return values

    def _load_status_aliases(self) -> Dict[str, str]:
        if self._status_aliases is not None:
            return self._status_aliases
        aliases: Dict[str, str] = {}
        try:
            list_payload = self.http.request(
                "GET",
                f"{self.api_base}/list/{self.list_id}",
                headers=self.headers,
            )
        except ProviderError:
            self._status_aliases = aliases
            return aliases
        for status in list_payload.get("statuses") or []:
            if not isinstance(status, Mapping):
                continue
            label = str(status.get("status") or status.get("name") or "").strip()
            if not label:
                continue
            aliases[label.lower()] = label
            for key in ("id", "status_id"):
                value = str(status.get(key) or "").strip()
                if value:
                    aliases[value.lower()] = label
        self._status_aliases = aliases
        return aliases

    @staticmethod
    def _task_matches_status(task: Mapping[str, Any], allowed_values: set[str]) -> bool:
        status = task.get("status") or {}
        if not isinstance(status, Mapping):
            return False
        for key in ("id", "status_id", "status", "name", "type"):
            value = str(status.get(key) or "").strip().lower()
            if value and value in allowed_values:
                return True
        return False

    @staticmethod
    def _normalize(task: Mapping[str, Any]) -> Dict[str, Any]:
        return normalized_task(
            provider="clickup",
            task_id=task["id"],
            name=task.get("name"),
            notes=task.get("description") or task.get("text_content") or "",
            url=task.get("url"),
            raw=task,
        )


class JiraProvider:
    def __init__(self, options: Mapping[str, str], *, dry_run: bool = False) -> None:
        self.options = dict(options)
        self.dry_run = dry_run
        self.api_base = self.options["JIRA_BASE_URL"].rstrip("/") + "/rest/api/3"
        self.http = HttpJsonClient()
        raw_token = f"{self.options['JIRA_EMAIL']}:{self.options['JIRA_API_TOKEN']}".encode("utf-8")
        self.auth_header = "Basic " + base64.b64encode(raw_token).decode("ascii")

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": self.auth_header}

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        jql = self.options.get("JIRA_READY_JQL")
        if not jql:
            project_key = self.options["JIRA_PROJECT_KEY"]
            jql = f'project = "{project_key}" AND status = "{section_gid}"'
        jql = jql_with_top_down_order(jql)
        response = self._search(jql)
        issues = response.get("issues") or []
        return self._normalize(issues[0]) if issues else None

    def get_task_contract(self, task_gid: str) -> Dict[str, Any]:
        issue = self.http.request(
            "GET",
            f"{self.api_base}/issue/{task_gid}",
            headers=self.headers,
            params={"fields": "summary,description,status,issuetype,project,labels"},
        )
        return self._normalize(issue)

    def move_task(self, task_gid: str, section_gid: str) -> None:
        transition_id = self._transition_id(task_gid, section_gid)
        if self.dry_run:
            print(f"[dry-run] Jira transition issue {task_gid} with transition {transition_id}")
            return
        self.http.request(
            "POST",
            f"{self.api_base}/issue/{task_gid}/transitions",
            headers=self.headers,
            json_body={"transition": {"id": transition_id}},
        )

    def comment(self, task_gid: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] Jira comment on issue {task_gid}: {text}")
            return
        self.http.request(
            "POST",
            f"{self.api_base}/issue/{task_gid}/comment",
            headers=self.headers,
            json_body={"body": jira_doc_text(text)},
        )

    def create_task(
        self,
        *,
        title: str,
        description: str,
        ready_state: str,
        tags: Sequence[str] = (),
        parent_task_id: str = "",
    ) -> Dict[str, Any]:
        project_key = self.options.get("JIRA_PROJECT_KEY")
        if not project_key:
            raise ProviderError("Jira task creation requires JIRA_PROJECT_KEY")
        fields: Dict[str, Any] = {
            "project": {"key": project_key},
            "summary": title,
            "description": jira_doc_text(description),
            "issuetype": {"name": self.options.get("JIRA_ISSUE_TYPE", "Task")},
        }
        if tags:
            fields["labels"] = [slug(tag).lower() for tag in tags]
        if self.dry_run:
            print(f"[dry-run] Jira create issue in project {project_key}: {title}")
            return normalized_task(provider="jira", task_id=f"dry-run-{slug(title)}", name=title, notes=description)
        issue = self.http.request(
            "POST",
            f"{self.api_base}/issue",
            headers=self.headers,
            json_body={"fields": fields},
        )
        issue_id = str(issue.get("key") or issue.get("id") or "")
        if not issue_id:
            raise ProviderError("Jira create issue response did not include an issue key")
        try:
            self.move_task(issue_id, ready_state)
        except ProviderError:
            pass
        return self.get_task_contract(issue_id)

    def _transition_id(self, task_gid: str, target: str) -> str:
        response = self.http.request(
            "GET",
            f"{self.api_base}/issue/{task_gid}/transitions",
            headers=self.headers,
        )
        transitions = response.get("transitions") or []
        target_lower = target.strip().lower()
        for transition in transitions:
            transition_id = str(transition.get("id", ""))
            transition_name = str(transition.get("name", ""))
            destination_name = str((transition.get("to") or {}).get("name") or "")
            if target in {transition_id, transition_name, destination_name}:
                return transition_id
            if target_lower in {transition_name.lower(), destination_name.lower()}:
                return transition_id
        available = ", ".join(
            f"{item.get('id')}:{item.get('name')}->{(item.get('to') or {}).get('name')}" for item in transitions
        )
        raise ProviderError(f"No Jira transition matched '{target}' for {task_gid}. Available: {available}")

    def _search(self, jql: str) -> Dict[str, Any]:
        body = {"jql": jql, "maxResults": 1, "fields": ["summary", "description", "status"]}
        configured = self.options.get("JIRA_SEARCH_ENDPOINT")
        endpoints = [configured] if configured else ["/search/jql", "/search"]
        last_error: Optional[ProviderError] = None
        for endpoint in endpoints:
            if not endpoint:
                continue
            try:
                return self.http.request("POST", self.api_base + endpoint, headers=self.headers, json_body=body)
            except ProviderError as exc:
                last_error = exc
                if configured or not any(marker in str(exc) for marker in ("HTTP 404", "HTTP 405", "HTTP 410")):
                    raise
        if last_error:
            raise last_error
        raise ProviderError("No Jira search endpoint configured")

    @staticmethod
    def _normalize(issue: Mapping[str, Any]) -> Dict[str, Any]:
        fields = issue.get("fields") or {}
        description = fields.get("description")
        return normalized_task(
            provider="jira",
            task_id=issue.get("key") or issue["id"],
            name=fields.get("summary"),
            notes=json.dumps(description) if isinstance(description, dict) else description or "",
            url=issue.get("self"),
            raw=issue,
        )


def jira_doc_text(text: str) -> Dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


class MondayProvider:
    def __init__(self, options: Mapping[str, str], *, dry_run: bool = False) -> None:
        self.options = dict(options)
        self.dry_run = dry_run
        self.board_id = self.options["MONDAY_BOARD_ID"]
        self.status_column_id = self.options["MONDAY_STATUS_COLUMN_ID"]
        self.http = HttpJsonClient()

    @property
    def headers(self) -> Dict[str, str]:
        headers = {"Authorization": self.options["MONDAY_API_TOKEN"]}
        if self.options.get("MONDAY_API_VERSION"):
            headers["API-Version"] = self.options["MONDAY_API_VERSION"]
        return headers

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Dict[str, Any]:
        response = self.http.request(
            "POST",
            MONDAY_API_BASE,
            headers=self.headers,
            json_body={"query": query, "variables": dict(variables)},
        )
        if response.get("errors"):
            raise ProviderError("monday.com API returned errors: " + json.dumps(response["errors"]))
        return response.get("data") or {}

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        data = self.graphql(
            """
query ($board_id: [ID!], $column_id: String!, $value: String!) {
  boards(ids: $board_id) {
    items_page_by_column_values(
      limit: 100,
      columns: [{column_id: $column_id, column_values: [$value]}]
    ) {
      items { id name url column_values { id text value } }
    }
  }
}
""",
            {"board_id": [self.board_id], "column_id": self.status_column_id, "value": section_gid},
        )
        boards = data.get("boards") or []
        items = ((boards[0] if boards else {}).get("items_page_by_column_values") or {}).get("items") or []
        return self._normalize(items[0]) if items else None

    def get_task_contract(self, task_gid: str) -> Dict[str, Any]:
        data = self.graphql(
            """
query ($ids: [ID!]) {
  items(ids: $ids) { id name url column_values { id text value } }
}
""",
            {"ids": [task_gid]},
        )
        items = data.get("items") or []
        if not items:
            raise ProviderError(f"monday.com item not found: {task_gid}")
        return self._normalize(items[0])

    def move_task(self, task_gid: str, section_gid: str) -> None:
        if self.dry_run:
            print(f"[dry-run] monday.com move item {task_gid} to status {section_gid}")
            return
        self.graphql(
            """
mutation ($board_id: ID!, $item_id: ID!, $column_id: String!, $value: JSON!) {
  change_column_value(board_id: $board_id, item_id: $item_id, column_id: $column_id, value: $value) { id }
}
""",
            {
                "board_id": self.board_id,
                "item_id": task_gid,
                "column_id": self.status_column_id,
                "value": json.dumps({"label": section_gid}),
            },
        )

    def comment(self, task_gid: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] monday.com update on item {task_gid}: {text}")
            return
        self.graphql(
            """
mutation ($item_id: ID!, $body: String!) {
  create_update(item_id: $item_id, body: $body) { id }
}
""",
            {"item_id": task_gid, "body": text},
        )

    def create_task(
        self,
        *,
        title: str,
        description: str,
        ready_state: str,
        tags: Sequence[str] = (),
        parent_task_id: str = "",
    ) -> Dict[str, Any]:
        notes = description
        if tags:
            notes += "\n\nTags: " + ", ".join(tags)
        if parent_task_id:
            notes += f"\n\nPlanned from: {parent_task_id}"
        column_values = {self.status_column_id: {"label": ready_state}}
        if self.dry_run:
            print(f"[dry-run] monday.com create item on board {self.board_id}: {title}")
            return normalized_task(provider="monday", task_id=f"dry-run-{slug(title)}", name=title, notes=notes)
        data = self.graphql(
            """
mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
  create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) { id name url }
}
""",
            {
                "board_id": self.board_id,
                "item_name": title,
                "column_values": json.dumps(column_values),
            },
        )
        item = data.get("create_item") or {}
        item_id = str(item.get("id") or "")
        if not item_id:
            raise ProviderError("monday.com create item response did not include an item id")
        self.comment(item_id, notes)
        return normalized_task(
            provider="monday",
            task_id=item_id,
            name=item.get("name") or title,
            notes=notes,
            url=item.get("url") or "",
            raw=item,
        )

    @staticmethod
    def _normalize(item: Mapping[str, Any]) -> Dict[str, Any]:
        notes = "\n".join(
            f"{column.get('id')}: {column.get('text')}"
            for column in item.get("column_values", [])
            if column.get("text")
        )
        return normalized_task(
            provider="monday",
            task_id=item["id"],
            name=item.get("name"),
            notes=notes,
            url=item.get("url"),
            raw=item,
        )


class CommandProvider:
    def __init__(self, options: Mapping[str, str], states: Mapping[str, str], *, dry_run: bool = False) -> None:
        self.options = dict(options)
        self.states = dict(states)
        self.dry_run = dry_run

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        payload = self._run_json(
            self.options["ORCHESTRATOR_COMMAND_NEXT_TASK"],
            {"ORCHESTRATOR_STATE": "ready", "ORCHESTRATOR_STATE_VALUE": section_gid},
            allow_empty=True,
        )
        return self._normalize_payload(payload) if payload else None

    def get_task_contract(self, task_gid: str) -> Dict[str, Any]:
        payload = self._run_json(
            self.options["ORCHESTRATOR_COMMAND_GET_TASK"],
            {"ORCHESTRATOR_TASK_ID": task_gid},
        )
        return self._normalize_payload(payload)

    def move_task(self, task_gid: str, section_gid: str) -> None:
        state = self._state_name(section_gid)
        if self.dry_run:
            print(f"[dry-run] command provider move {task_gid} to {state}:{section_gid}")
            return
        self._run_text(
            self.options["ORCHESTRATOR_COMMAND_MOVE_TASK"],
            {
                "ORCHESTRATOR_TASK_ID": task_gid,
                "ORCHESTRATOR_STATE": state,
                "ORCHESTRATOR_STATE_VALUE": section_gid,
            },
        )

    def comment(self, task_gid: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] command provider comment on {task_gid}: {text}")
            return
        self._run_text(
            self.options["ORCHESTRATOR_COMMAND_COMMENT_TASK"],
            {"ORCHESTRATOR_TASK_ID": task_gid, "ORCHESTRATOR_COMMENT_TEXT": text},
        )

    def create_task(
        self,
        *,
        title: str,
        description: str,
        ready_state: str,
        tags: Sequence[str] = (),
        parent_task_id: str = "",
    ) -> Dict[str, Any]:
        command = self.options.get("ORCHESTRATOR_COMMAND_CREATE_TASK")
        if not command:
            raise ProviderError("Command provider task creation requires ORCHESTRATOR_COMMAND_CREATE_TASK")
        payload = {
            "title": title,
            "description": description,
            "ready_state": ready_state,
            "tags": list(tags),
            "parent_task_id": parent_task_id,
        }
        if self.dry_run:
            print(f"[dry-run] command provider create task: {title}")
            return normalized_task(provider="command", task_id=f"dry-run-{slug(title)}", name=title, notes=description)
        created = self._run_json(
            command,
            {
                "ORCHESTRATOR_TASK_CREATE_JSON": json.dumps(payload),
                "ORCHESTRATOR_TASK_TITLE": title,
                "ORCHESTRATOR_TASK_DESCRIPTION": description,
                "ORCHESTRATOR_TASK_TAGS": ",".join(tags),
                "ORCHESTRATOR_TASK_PARENT_ID": parent_task_id,
                "ORCHESTRATOR_STATE": "ready",
                "ORCHESTRATOR_STATE_VALUE": ready_state,
            },
        )
        if not created:
            raise ProviderError("Command provider create task command returned no task payload")
        return self._normalize_payload(created)

    def _run_json(
        self,
        command: str,
        extra_env: Mapping[str, str],
        *,
        allow_empty: bool = False,
    ) -> Optional[Dict[str, Any]]:
        output = self._run_text(command, extra_env)
        if allow_empty and not output.strip():
            return None
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Command provider returned invalid JSON: {exc}\n{output}") from exc
        if payload is None and allow_empty:
            return None
        if not isinstance(payload, dict):
            raise ProviderError("Command provider JSON output must be an object or null")
        return payload

    @staticmethod
    def _run_text(command: str, extra_env: Mapping[str, str]) -> str:
        env = os.environ.copy()
        env.update(extra_env)
        expanded_command = expand_command_env(command, env)
        completed = subprocess.run(
            expanded_command,
            shell=True,
            text=True,
            encoding=PROCESS_ENCODING,
            errors=PROCESS_ERRORS,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if completed.returncode != 0:
            raise ProviderError(
                f"Command provider command failed ({completed.returncode}): {command}\n{completed.stdout}"
            )
        return completed.stdout or ""

    def _state_name(self, section_gid: str) -> str:
        for name, value in self.states.items():
            if value == section_gid:
                return name
        return "unknown"

    @staticmethod
    def _normalize_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
        task_id = payload.get("gid") or payload.get("id") or payload.get("key")
        if not task_id:
            raise ProviderError("Command provider task payload must include gid, id, or key")
        if "gid" in payload and "name" in payload:
            return dict(payload)
        normalized = normalized_task(
            provider=str(payload.get("provider") or "command"),
            task_id=task_id,
            name=payload.get("name") or payload.get("title") or payload.get("summary") or "",
            notes=payload.get("notes") or payload.get("description") or "",
            url=payload.get("permalink_url") or payload.get("url") or "",
            raw=payload,
        )
        normalized.update({key: value for key, value in payload.items() if key not in normalized})
        return normalized


def build_provider(config: OrchestratorConfig) -> Any:
    if config.provider_name == "asana":
        return AsanaClient(
            config.provider_options["ASANA_ACCESS_TOKEN"],
            dry_run=config.dry_run,
            project_gid=config.project_gid,
        )
    if config.provider_name == "trello":
        return TrelloProvider(
            config.provider_options["TRELLO_API_KEY"],
            config.provider_options["TRELLO_TOKEN"],
            dry_run=config.dry_run,
        )
    if config.provider_name == "clickup":
        return ClickUpProvider(
            config.provider_options["CLICKUP_ACCESS_TOKEN"],
            config.provider_options["CLICKUP_LIST_ID"],
            dry_run=config.dry_run,
            api_base=config.provider_options.get("CLICKUP_API_BASE", CLICKUP_API_BASE),
            view_id=config.provider_options.get("CLICKUP_VIEW_ID", ""),
            task_order=config.provider_options.get("CLICKUP_TASK_ORDER", "api"),
            task_reverse=config.provider_options.get("CLICKUP_TASK_REVERSE", "true"),
        )
    if config.provider_name == "jira":
        return JiraProvider(config.provider_options, dry_run=config.dry_run)
    if config.provider_name == "monday":
        return MondayProvider(config.provider_options, dry_run=config.dry_run)
    if config.provider_name == "command":
        return CommandProvider(config.provider_options, config.sections, dry_run=config.dry_run)
    raise ConfigError(f"Unsupported provider: {config.provider_name}")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        return read_json(self.path)

    def active(self) -> Optional[Dict[str, Any]]:
        return self.load().get("active_attempt")

    def save_active(self, payload: Mapping[str, Any]) -> None:
        state = self.load()
        state["active_attempt"] = dict(payload)
        state["updated_at"] = utc_now()
        atomic_write_json(self.path, state)

    def record_stage(self, stage: str, outcome: "StageOutcome") -> None:
        state = self.load()
        active = state.get("active_attempt") or {}
        stages = active.setdefault("stages", {})
        stages[stage] = {
            "status": outcome.status,
            "summary": outcome.summary,
            "report_path": str(outcome.report_path),
            "log_path": str(outcome.log_path),
            "retryable": outcome.retryable,
            "finished_at": utc_now(),
        }
        active["stage"] = stage
        active["updated_at"] = utc_now()
        state["active_attempt"] = active
        state["updated_at"] = utc_now()
        atomic_write_json(self.path, state)

    def clear_active(self) -> None:
        state = self.load()
        state.pop("active_attempt", None)
        state["updated_at"] = utc_now()
        atomic_write_json(self.path, state)


def _read_reset_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read attempt state {path}: {exc}; replacing it.")
        return {}


def _clear_attempt_state(path: Path, *, dry_run: bool) -> bool:
    if not path.exists():
        print(f"No attempt state file found at {path}")
        return False
    if dry_run:
        print(f"Would clear active attempt state in {path}")
        return True

    state = _read_reset_state(path)
    state.pop("active_attempt", None)
    state["updated_at"] = utc_now()
    atomic_write_json(path, state)
    print(f"Cleared active attempt state in {path}")
    return True


def _git_attempt_worktrees(config: AttemptResetConfig) -> List[Path]:
    completed = run_local(["git", "worktree", "list", "--porcelain"], cwd=config.repo_root, check=False)
    if completed.returncode != 0:
        return []

    paths: List[Path] = []
    for line in completed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree ") :].strip()).resolve()
        if "-attempt-" not in path.name:
            continue
        try:
            ensure_under(path, config.worktree_root)
        except ConfigError:
            continue
        paths.append(path)
    return sorted(set(paths), key=str)


def _attempt_branches(repo_root: Path) -> List[str]:
    completed = run_local(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/orchestrator"],
        cwd=repo_root,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return sorted(line.strip() for line in completed.stdout.splitlines() if "-attempt-" in line)


def _remove_tree(path: Path, root: Path, *, label: str, dry_run: bool) -> bool:
    ensure_under(path, root)
    if dry_run:
        print(f"Would remove {label} {path}")
        return True

    print(f"Removing {label} {path}")
    error = rmtree_with_retries(path)
    if error and path.exists():
        print(f"Warning: could not remove {label} {path}: {error}")
        return False
    return True


def _stale_attempt_worktree_dirs(worktree_root: Path) -> List[Path]:
    if not worktree_root.exists():
        return []
    return sorted(
        (path.resolve() for path in worktree_root.iterdir() if path.is_dir() and "-attempt-" in path.name),
        key=str,
    )


def _attempt_artifact_dirs(artifact_root: Path) -> List[Path]:
    if not artifact_root.exists():
        return []
    return sorted(
        (path.resolve() for path in artifact_root.rglob("attempt-*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )


def _remove_empty_artifact_dirs(artifact_root: Path) -> None:
    if not artifact_root.exists():
        return
    dirs = sorted(
        (path for path in artifact_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in dirs:
        try:
            ensure_under(path, artifact_root)
            path.rmdir()
        except (ConfigError, OSError):
            pass


def reset_attempts(config: AttemptResetConfig, *, dry_run: bool = False) -> Dict[str, int]:
    counts = {
        "state_files": 0,
        "worktrees": 0,
        "stale_worktree_dirs": 0,
        "branches": 0,
        "artifact_dirs": 0,
        "failures": 0,
    }
    action = "Planning reset of" if dry_run else "Resetting"
    print(f"{action} local workflow attempts for {config.repo_root}")

    state_path = config.artifact_root / "orchestrator_state.json"
    if _clear_attempt_state(state_path, dry_run=dry_run):
        counts["state_files"] += 1

    for path in _git_attempt_worktrees(config):
        if dry_run:
            print(f"Would remove git worktree {path}")
            counts["worktrees"] += 1
            continue
        print(f"Removing git worktree {path}")
        run_local(["git", "worktree", "remove", "--force", str(path)], cwd=config.repo_root, check=False)
        if path.exists() and not _remove_tree(path, config.worktree_root, label="worktree directory", dry_run=False):
            counts["failures"] += 1
        else:
            counts["worktrees"] += 1

    if not dry_run:
        run_local(["git", "worktree", "prune"], cwd=config.repo_root, check=False)

    for path in _stale_attempt_worktree_dirs(config.worktree_root):
        if _remove_tree(path, config.worktree_root, label="stale worktree directory", dry_run=dry_run):
            counts["stale_worktree_dirs"] += 1
        else:
            counts["failures"] += 1

    if not dry_run:
        run_local(["git", "worktree", "prune"], cwd=config.repo_root, check=False)

    for branch in _attempt_branches(config.repo_root):
        if dry_run:
            print(f"Would delete branch {branch}")
            counts["branches"] += 1
            continue
        print(f"Deleting branch {branch}")
        completed = run_local(["git", "branch", "-D", branch], cwd=config.repo_root, check=False)
        if completed.returncode == 0:
            counts["branches"] += 1
        elif completed.stdout.strip():
            print(f"Warning: could not delete branch {branch}: {completed.stdout.strip()}")
            counts["failures"] += 1

    for path in _attempt_artifact_dirs(config.artifact_root):
        if path.exists() and _remove_tree(path, config.artifact_root, label="attempt artifacts", dry_run=dry_run):
            counts["artifact_dirs"] += 1
        elif path.exists():
            counts["failures"] += 1

    if not dry_run:
        _remove_empty_artifact_dirs(config.artifact_root)

    status = "complete" if counts["failures"] == 0 else "incomplete"
    print(
        f"Attempt reset {status}: "
        f"{counts['state_files']} state file(s), "
        f"{counts['worktrees']} git worktree(s), "
        f"{counts['stale_worktree_dirs']} stale worktree dir(s), "
        f"{counts['branches']} branch(es), "
        f"{counts['artifact_dirs']} artifact dir(s), "
        f"{counts['failures']} failure(s)."
    )
    return counts


def _normalized_tag(value: Any) -> str:
    return str(value or "").strip().lower()


def task_tag_values(task_contract: Mapping[str, Any]) -> List[str]:
    values: List[str] = []

    def collect(candidate: Any) -> None:
        if isinstance(candidate, str):
            if candidate.strip():
                values.append(candidate.strip())
            return
        if isinstance(candidate, Mapping):
            for key in ("name", "tag", "id", "gid"):
                value = candidate.get(key)
                if value is not None and str(value).strip():
                    values.append(str(value).strip())
            return
        if isinstance(candidate, IterableABC) and not isinstance(candidate, (str, bytes)):
            for item in candidate:
                collect(item)

    collect(task_contract.get("tags"))
    raw = task_contract.get("raw")
    if isinstance(raw, Mapping):
        collect(raw.get("tags"))
        collect(raw.get("labels"))
    return values


def task_has_deploy_tag(task_contract: Mapping[str, Any], deploy_tag: Optional[str]) -> bool:
    expected = _normalized_tag(deploy_tag)
    if not expected:
        return False
    return any(_normalized_tag(value) == expected for value in task_tag_values(task_contract))


def task_text(task_contract: Mapping[str, Any]) -> str:
    parts = [
        str(task_contract.get("name") or ""),
        str(task_contract.get("notes") or ""),
        str(task_contract.get("description") or ""),
        str(task_contract.get("text_content") or ""),
    ]
    raw = task_contract.get("raw")
    if isinstance(raw, Mapping):
        parts.extend(
            [
                str(raw.get("name") or ""),
                str(raw.get("description") or ""),
                str(raw.get("text_content") or ""),
            ]
        )
    return "\n".join(part for part in parts if part)


def task_has_planner_signal(task_contract: Mapping[str, Any], planner_tag: Optional[str]) -> bool:
    expected = _normalized_tag(planner_tag)
    if expected and any(_normalized_tag(value) == expected for value in task_tag_values(task_contract)):
        return True
    return bool(PLANNING_TASK_PATTERN.search(task_text(task_contract)))


def normalize_plan_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,\n]+", value) if part.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def plan_task_description(item: Mapping[str, Any], *, source_task: Mapping[str, Any]) -> str:
    description = str(item.get("description") or item.get("notes") or "").strip()
    acceptance = item.get("acceptance_criteria") or item.get("acceptance") or []
    if isinstance(acceptance, str):
        acceptance_items = [line.strip("- ").strip() for line in acceptance.splitlines() if line.strip()]
    elif isinstance(acceptance, Sequence) and not isinstance(acceptance, (str, bytes, bytearray)):
        acceptance_items = [str(line).strip() for line in acceptance if str(line).strip()]
    else:
        acceptance_items = []

    parts = [description]
    if acceptance_items:
        parts.append("Acceptance criteria:\n" + "\n".join(f"- {line}" for line in acceptance_items))
    source_url = str(source_task.get("permalink_url") or source_task.get("url") or "").strip()
    source_name = str(source_task.get("name") or source_task.get("gid") or "").strip()
    source_line = f"Planned from: {source_name}"
    if source_url:
        source_line += f" ({source_url})"
    parts.append(source_line)
    return "\n\n".join(part for part in parts if part)


def load_task_plan(outcome: "StageOutcome", plan_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    payload: Any
    if plan_path.exists():
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return [], [f"invalid task plan JSON at {plan_path}: {exc}"]
    else:
        payload = outcome.report.get("task_plan") or outcome.report.get("tasks")

    raw_tasks = payload.get("tasks") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_tasks, list):
        return [], ["task plan must be a JSON object with a tasks array or a tasks array"]

    tasks: List[Dict[str, Any]] = []
    failures: List[str] = []
    for index, raw_item in enumerate(raw_tasks, start=1):
        if not isinstance(raw_item, Mapping):
            failures.append(f"task plan item {index} must be an object")
            continue
        title = str(raw_item.get("title") or raw_item.get("name") or raw_item.get("summary") or "").strip()
        description = str(raw_item.get("description") or raw_item.get("notes") or "").strip()
        if not title:
            failures.append(f"task plan item {index} missing title")
        if not description:
            failures.append(f"task plan item {index} missing description")
        if not title or not description:
            continue
        item = dict(raw_item)
        item["title"] = title
        item["description"] = description
        item["tags"] = normalize_plan_tags(item.get("tags"))
        tasks.append(item)
    if not tasks and not failures:
        failures.append("task plan did not include any tasks")
    return tasks, failures


def created_tasks_next_action(created_tasks: Sequence[Mapping[str, Any]]) -> str:
    if not created_tasks:
        return "New tasks are in the Ready queue."
    labels: List[str] = []
    for task in created_tasks[:8]:
        title = str(task.get("title") or task.get("id") or "task")
        url = str(task.get("url") or "").strip()
        labels.append(f"{title} ({url})" if url else title)
    suffix = "" if len(created_tasks) <= 8 else f"; plus {len(created_tasks) - 8} more"
    return "Created Ready tasks: " + "; ".join(labels) + suffix + "."


def _task_deploy_text(task_contract: Mapping[str, Any]) -> str:
    parts = [
        str(task_contract.get("name") or ""),
        str(task_contract.get("notes") or ""),
        str(task_contract.get("description") or ""),
        str(task_contract.get("text_content") or ""),
    ]
    raw = task_contract.get("raw")
    if isinstance(raw, Mapping):
        parts.extend(
            [
                str(raw.get("name") or ""),
                str(raw.get("description") or ""),
                str(raw.get("text_content") or ""),
            ]
        )
    return "\n".join(part for part in parts if part)


def task_requests_deploy(task_contract: Mapping[str, Any]) -> Optional[bool]:
    text = _task_deploy_text(task_contract)
    if DEPLOY_SKIP_PATTERN.search(text):
        return False
    if DEPLOY_FORCE_PATTERN.search(text):
        return True
    return None


def report_requests_deploy(report: Mapping[str, Any]) -> Optional[bool]:
    for key in ("deploy_required", "requires_deploy"):
        if key in report:
            return bool(report[key])
    for key in ("skip_deploy", "skip_deployment", "skip_smoke"):
        if key in report and bool(report[key]):
            return False
    return None


def report_changed_files(report: Mapping[str, Any]) -> List[str]:
    changed = report.get("changed_files") or []
    if isinstance(changed, Mapping):
        changed = changed.values()
    if not isinstance(changed, IterableABC) or isinstance(changed, (str, bytes)):
        return []
    return [str(path) for path in changed if str(path).strip()]


def changed_files_since_base(worktree_path: Path, base_ref: str) -> List[str]:
    if base_ref == "HEAD":
        return []
    completed = run_local(["git", "diff", "--name-only", f"{base_ref}...HEAD"], cwd=worktree_path, check=False)
    if completed.returncode != 0:
        completed = run_local(["git", "diff", "--name-only", base_ref, "HEAD"], cwd=worktree_path, check=False)
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def should_run_deploy(
    task_contract: Mapping[str, Any],
    build: "StageOutcome",
    verify: "StageOutcome",
    *,
    worktree_path: Path,
    base_ref: str,
) -> Tuple[bool, str]:
    for report in (verify.report, build.report):
        report_decision = report_requests_deploy(report)
        if report_decision is False:
            return False, "stage report requested deploy/smoke skip"
        if report_decision is True:
            return True, "stage report requested deploy"

    task_decision = task_requests_deploy(task_contract)
    if task_decision is False:
        return False, "task requested deploy/smoke skip"
    if task_decision is True:
        return True, "task requested deploy"

    changed = report_changed_files(build.report) or changed_files_since_base(worktree_path, base_ref)
    if not changed:
        return False, "no changed files to deploy"
    return True, "changed files require deploy"


def read_stage_report(attempt_artifact_dir: Path, stage: str) -> Dict[str, Any]:
    report_path = attempt_artifact_dir / stage / f"{stage}_report.json"
    if not report_path.exists():
        return {}
    try:
        return read_json(report_path)
    except (OSError, json.JSONDecodeError):
        return {}


def deployer_skip_reason(
    task_contract: Mapping[str, Any],
    attempt_artifact_dir: Path,
    worktree_path: Path,
    base_ref: str,
    *,
    deploy_policy: str = "all",
    deploy_tag: Optional[str] = None,
) -> Optional[str]:
    if deploy_policy == "none":
        return "deployment policy is none"
    if deploy_policy == "tagged" and not task_has_deploy_tag(task_contract, deploy_tag):
        return f"task does not have deployment tag '{deploy_tag}'"

    task_decision = task_requests_deploy(task_contract)
    if task_decision is False:
        return "task requested deploy/smoke skip"
    if task_decision is True:
        return None

    reports = [read_stage_report(attempt_artifact_dir, "verifier"), read_stage_report(attempt_artifact_dir, "builder")]
    for report in reports:
        report_decision = report_requests_deploy(report)
        if report_decision is False:
            return "stage report requested deploy/smoke skip"
        if report_decision is True:
            return None

    return None


class WorktreeManager:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

    def branch_for_path(self, path: Path) -> str:
        return f"orchestrator/{path.name}"

    def create(self, task_id: str, attempt: int, *, stage: str = "stage", base_ref: Optional[str] = None) -> Tuple[Path, str]:
        safe_task = slug(task_id)
        safe_stage = slug(stage)
        timestamp = compact_timestamp()
        self.config.worktree_root.mkdir(parents=True, exist_ok=True)
        path = self.config.worktree_root / f"{safe_task}-attempt-{attempt}-{safe_stage}-{timestamp}"
        branch = f"orchestrator/{safe_task}-attempt-{attempt}-{safe_stage}-{timestamp}"
        source_ref = base_ref or self.config.base_branch
        print(f"Creating {stage} worktree {path} from {source_ref}")
        run_local(
            ["git", "worktree", "add", "-b", branch, str(path), source_ref],
            cwd=self.config.repo_root,
        )
        return path.resolve(), branch

    def current_commit(self, worktree_path: Path) -> str:
        return run_local(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    def checkpoint(self, worktree_path: Path, *, task_id: str, attempt: int, stage: str) -> str:
        status = run_local(["git", "status", "--porcelain"], cwd=worktree_path).stdout.strip()
        if status:
            run_local(["git", "add", "-A"], cwd=worktree_path)
            message = f"orchestrator checkpoint: {task_id} attempt {attempt} {stage}"
            run_local(
                [
                    "git",
                    "-c",
                    "user.name=PATBTAWO",
                    "-c",
                    "user.email=patbtawo@example.invalid",
                    "commit",
                    "-m",
                    message,
                ],
                cwd=worktree_path,
            )
        return self.current_commit(worktree_path)

    def merge_into_base(self, ref: str) -> None:
        if self.config.base_branch == "HEAD":
            raise ConfigError(
                "ORCHESTRATOR_BASE_BRANCH resolved to HEAD, so PATBTAWO cannot merge task changes back "
                "into a named base branch. Set ORCHESTRATOR_BASE_BRANCH to a branch such as main."
            )
        current = run_local(["git", "branch", "--show-current"], cwd=self.config.repo_root, check=False).stdout.strip()
        if current != self.config.base_branch:
            run_local(["git", "switch", self.config.base_branch], cwd=self.config.repo_root)
        print(f"Fast-forward merging {ref} into {self.config.base_branch}")
        run_local(["git", "merge", "--ff-only", ref], cwd=self.config.repo_root)

    def remove(self, path: Path) -> None:
        ensure_under(path, self.config.worktree_root)
        if self.config.keep_worktrees:
            print(f"Keeping worktree due to ORCHESTRATOR_KEEP_WORKTREES: {path}")
            return
        if not path.exists():
            self._delete_branch(self.branch_for_path(path))
            return
        print(f"Removing worktree {path}")
        run_local(["git", "worktree", "remove", "--force", str(path)], cwd=self.config.repo_root, check=False)
        if path.exists():
            error = rmtree_with_retries(path)
            if error and path.exists():
                print(
                    "Warning: could not remove worktree "
                    f"{path}: {error}. Close any shells, editors, or scanners using it; "
                    "PATBTAWO will continue with a fresh worktree."
                )
        run_local(["git", "worktree", "prune"], cwd=self.config.repo_root, check=False)
        self._delete_branch(self.branch_for_path(path))

    def _delete_branch(self, branch: str) -> None:
        if not branch:
            return
        print(f"Deleting branch {branch}")
        run_local(["git", "branch", "-D", branch], cwd=self.config.repo_root, check=False)


@dataclasses.dataclass
class StageOutcome:
    stage: str
    status: str
    summary: str
    report: Dict[str, Any]
    report_path: Path
    log_path: Path
    retryable: bool
    exit_code: Optional[int]

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


def stage_report_summary(outcome: "StageOutcome") -> Dict[str, Any]:
    report = outcome.report if isinstance(outcome.report, Mapping) else {}
    return {
        "stage": outcome.stage,
        "status": outcome.status,
        "summary": outcome.summary,
        "changed_files": report.get("changed_files") or [],
        "checks": report.get("checks") or [],
        "failures": report.get("failures") or [],
        "next_action": str(report.get("next_action", "")),
        "artifact_paths": report.get("artifact_paths") or [],
        "report_path": str(outcome.report_path),
        "log_path": str(outcome.log_path),
    }


class StageRunner:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        stage: str,
        command: str,
        task_contract: Mapping[str, Any],
        attempt: int,
        worktree_path: Path,
        attempt_artifact_dir: Path,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> StageOutcome:
        stage_dir = attempt_artifact_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        report_path = stage_dir / f"{stage}_report.json"
        log_path = stage_dir / f"{stage}.log"
        contract_path = stage_dir / "task_contract.json"
        task_plan_path = stage_dir / "task_plan.json"
        atomic_write_json(contract_path, task_contract)

        task_id = str(task_contract["gid"])
        if stage == "deployer":
            skip_reason = deployer_skip_reason(
                task_contract,
                attempt_artifact_dir,
                worktree_path,
                self.config.base_branch,
                deploy_policy=self.config.deploy_policy,
                deploy_tag=self.config.deploy_tag,
            )
            if skip_reason:
                summary = f"Skipped deploy/smoke because {skip_reason}."
                report = {
                    "task_id": task_id,
                    "status": "success",
                    "summary": summary,
                    "changed_files": [],
                    "checks": [{"name": "deploy policy", "status": "skipped", "details": skip_reason}],
                    "failures": [],
                    "next_action": "Continue without deploy.",
                    "artifact_paths": [str(log_path)],
                    "deploy_skipped": True,
                    "deploy_skip_reason": skip_reason,
                }
                log_path.write_text(summary + "\n", encoding="utf-8")
                atomic_write_json(report_path, report)
                return StageOutcome(
                    stage=stage,
                    status="success",
                    summary=summary,
                    report=report,
                    report_path=report_path,
                    log_path=log_path,
                    retryable=False,
                    exit_code=0,
                )

        subagent_id = f"{slug(task_id)}-{attempt}-{slug(stage)}-{compact_timestamp()}"
        env = os.environ.copy()
        env.update(self.config.stage_environment)
        env.update(
            {
                "ORCHESTRATOR_STAGE": stage,
                "ORCHESTRATOR_SUBAGENT_ID": subagent_id,
                "ORCHESTRATOR_SUBAGENT_ROLE": stage,
                "ORCHESTRATOR_TASK_ID": task_id,
                "ORCHESTRATOR_TASK_NAME": str(task_contract.get("name", "")),
                "ORCHESTRATOR_TASK_CONTRACT_PATH": str(contract_path),
                "ASANA_TASK_CONTRACT_PATH": str(contract_path),
                "ORCHESTRATOR_REPORT_PATH": str(report_path),
                "ORCHESTRATOR_LOG_PATH": str(log_path),
                "ORCHESTRATOR_TASK_PLAN_PATH": str(task_plan_path),
                "ORCHESTRATOR_ARTIFACT_DIR": str(stage_dir),
                "ORCHESTRATOR_ATTEMPT_ARTIFACT_DIR": str(attempt_artifact_dir),
                "ORCHESTRATOR_CONTEXT_PATH": str(self.config.context_path),
                "ORCHESTRATOR_ATTEMPT": str(attempt),
                "ORCHESTRATOR_PROJECT_GID": self.config.project_gid,
                "ORCHESTRATOR_PROJECT_ID": self.config.project_gid,
                "ORCHESTRATOR_PROVIDER": self.config.provider_name,
                "ORCHESTRATOR_STAGE_WORKTREE_PATH": str(worktree_path),
                "ORCHESTRATOR_OBJECTIVE_VERIFIER": "true" if self.config.objective_verifier else "false",
            }
        )
        if extra_env:
            env.update(extra_env)

        started_at = time.monotonic()
        header = [
            f"stage={stage}",
            f"subagent_id={subagent_id}",
            f"task_id={task_id}",
            f"attempt={attempt}",
            f"cwd={worktree_path}",
            f"command={command}",
            f"started_at={utc_now()}",
            "",
        ]
        output = ""
        exit_code: Optional[int] = None
        timed_out = False
        expanded_command = expand_command_env(command, env)
        try:
            completed = subprocess.run(
                expanded_command,
                cwd=str(worktree_path),
                env=env,
                shell=True,
                text=True,
                encoding=PROCESS_ENCODING,
                errors=PROCESS_ERRORS,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.config.stage_timeout_seconds,
            )
            output = completed.stdout or ""
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            output += f"\nTimed out after {self.config.stage_timeout_seconds} seconds.\n"
            exit_code = None
            timed_out = True

        duration = time.monotonic() - started_at
        log_path.write_text(
            "\n".join(header)
            + output
            + f"\nfinished_at={utc_now()}\n"
            + f"duration_seconds={duration:.3f}\n"
            + f"exit_code={exit_code}\n",
            encoding="utf-8",
        )

        report, load_failures = self._load_stage_report(report_path, task_id)
        validation_failures = validate_report(report, task_id) if report else load_failures
        report_status = normalize_status(report.get("status")) if report else "unknown"

        command_failed = exit_code not in (0, None)
        if timed_out:
            validation_failures.insert(0, "stage command timed out")
        if command_failed and report_status not in {"failed", "blocked"}:
            validation_failures.insert(0, f"stage command exited with code {exit_code}")
        tail = output_tail(output)
        if tail and validation_failures:
            validation_failures.insert(1 if command_failed else 0, f"stage output: {tail}")
        if exit_code not in (0, None) and report_status == "success":
            validation_failures.append("stage command exited non-zero while report status was success")
        if exit_code == 0 and report_status == "unknown":
            validation_failures.append("stage report status is not recognized")

        if validation_failures:
            raw_retryable = bool(report.get("retryable") or report.get("transient")) if report else False
            retryable = raw_retryable or timed_out or exit_code in TRANSIENT_EXIT_CODES
            report = failure_report(
                task_id=task_id,
                stage=stage,
                summary=f"{stage} did not produce a valid passing report",
                failures=validation_failures,
                next_action=f"Fix {stage} command/report generation and rerun the orchestrator.",
                artifact_paths=[str(log_path)],
                retryable=retryable,
            )
            report["subagent_id"] = subagent_id
            report["worktree_path"] = str(worktree_path)
            atomic_write_json(report_path, report)
            return StageOutcome(
                stage=stage,
                status="failed",
                summary=str(report["summary"]),
                report=report,
                report_path=report_path,
                log_path=log_path,
                retryable=retryable,
                exit_code=exit_code,
            )

        status = report_status
        retryable = bool(report.get("retryable") or report.get("transient") or exit_code in TRANSIENT_EXIT_CODES)
        if command_failed and status != "blocked":
            status = "failed"
            report["status"] = "failed"
        if command_failed:
            failures = report.get("failures")
            if isinstance(failures, list) and not failures:
                failures.append(f"stage command exited with code {exit_code}")
        report.setdefault("stage", stage)
        report.setdefault("attempt", attempt)
        report.setdefault("subagent_id", subagent_id)
        report.setdefault("worktree_path", str(worktree_path))
        report.setdefault("command_exit_code", exit_code)
        report.setdefault("duration_seconds", round(duration, 3))
        artifact_paths = report.get("artifact_paths")
        if isinstance(artifact_paths, list) and str(log_path) not in artifact_paths:
            artifact_paths.append(str(log_path))
        atomic_write_json(report_path, report)
        return StageOutcome(
            stage=stage,
            status=status,
            summary=str(report.get("summary", "")),
            report=report,
            report_path=report_path,
            log_path=log_path,
            retryable=retryable,
            exit_code=exit_code,
        )

    @staticmethod
    def _load_stage_report(report_path: Path, task_id: str) -> Tuple[Dict[str, Any], List[str]]:
        if not report_path.exists():
            return {}, [f"missing report file: {report_path}"]
        try:
            report = read_json(report_path)
        except json.JSONDecodeError as exc:
            invalid_path = report_path.with_suffix(report_path.suffix + ".invalid")
            shutil.copyfile(report_path, invalid_path)
            return {}, [f"invalid JSON report at {report_path}: {exc}"]
        if not isinstance(report, dict):
            return {}, [f"report must be a JSON object for task {task_id}"]
        return report, []


def normalize_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in SUCCESS_STATUSES:
        return "success"
    if normalized in FAILED_STATUSES:
        return "failed"
    if normalized in BLOCKED_STATUSES:
        return "blocked"
    return "unknown"


def validate_report(report: Mapping[str, Any], task_id: str) -> List[str]:
    failures: List[str] = []
    for key in REQUIRED_REPORT_KEYS:
        if key not in report:
            failures.append(f"report missing required key: {key}")
    if failures:
        return failures
    if str(report["task_id"]) != str(task_id):
        failures.append(f"report task_id {report['task_id']} does not match {task_id}")
    if normalize_status(report["status"]) == "unknown":
        failures.append(f"report status is not recognized: {report['status']}")
    if not isinstance(report["summary"], str):
        failures.append("report summary must be a string")
    if not isinstance(report["changed_files"], list):
        failures.append("report changed_files must be a list")
    if not isinstance(report["checks"], (list, dict)):
        failures.append("report checks must be a list or object")
    if not isinstance(report["failures"], list):
        failures.append("report failures must be a list")
    if not isinstance(report["next_action"], str):
        failures.append("report next_action must be a string")
    if not isinstance(report["artifact_paths"], (list, dict)):
        failures.append("report artifact_paths must be a list or object")
    return failures


def failure_report(
    *,
    task_id: str,
    stage: str,
    summary: str,
    failures: Iterable[str],
    next_action: str,
    artifact_paths: Iterable[str],
    retryable: bool = False,
    status: str = "failed",
) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "changed_files": [],
        "checks": [{"name": f"{stage}_report_validation", "status": "failed"}],
        "failures": list(failures),
        "next_action": next_action,
        "artifact_paths": list(artifact_paths),
        "retryable": retryable,
        "generated_by": "task_orchestrator",
        "generated_at": utc_now(),
    }


def synthetic_stage_outcome(
    *,
    stage: str,
    task_id: str,
    attempt: int,
    attempt_artifact_dir: Path,
    summary: str,
    failures: Iterable[str],
    next_action: str,
    retryable: bool = False,
) -> StageOutcome:
    stage_dir = attempt_artifact_dir / stage
    stage_dir.mkdir(parents=True, exist_ok=True)
    report_path = stage_dir / f"{stage}_report.json"
    log_path = stage_dir / f"{stage}.log"
    report = failure_report(
        task_id=task_id,
        stage=stage,
        summary=summary,
        failures=list(failures),
        next_action=next_action,
        artifact_paths=[str(log_path)],
        retryable=retryable,
    )
    report["stage"] = stage
    report["attempt"] = attempt
    atomic_write_json(report_path, report)
    log_path.write_text(
        "\n".join(
            [
                f"stage={stage}",
                f"task_id={task_id}",
                f"attempt={attempt}",
                f"summary={summary}",
                "failures:",
                *[f"- {failure}" for failure in report["failures"]],
                f"finished_at={utc_now()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return StageOutcome(
        stage=stage,
        status="failed",
        summary=summary,
        report=report,
        report_path=report_path,
        log_path=log_path,
        retryable=retryable,
        exit_code=None,
    )


def tracked_changed_files(worktree_path: Path) -> List[str]:
    changed: List[str] = []
    for args in (["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]):
        output = run_local(args, cwd=worktree_path).stdout
        changed.extend(line.strip() for line in output.splitlines() if line.strip())
    return sorted(set(changed))


def ensure_sentence(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return ""
    return trimmed if trimmed[-1] in ".!?" else trimmed + "."


def strip_sentence_end(value: str) -> str:
    return value.strip().rstrip(".!?").strip()


def summarize_failures(failures: Optional[Sequence[Any]], *, limit: int = 2) -> str:
    if not failures:
        return ""
    summaries: List[str] = []
    for failure in failures:
        if isinstance(failure, Mapping):
            text = str(
                failure.get("summary")
                or failure.get("message")
                or failure.get("reason")
                or failure.get("name")
                or json.dumps(failure, sort_keys=True)
            )
        else:
            text = str(failure)
        text = " ".join(text.split())
        if text:
            summaries.append(strip_sentence_end(text))
        if len(summaries) >= limit:
            break
    if not summaries:
        return ""
    if len(failures) > limit:
        summaries.append(f"{len(failures) - limit} more issue(s)")
    return "; ".join(summaries)[:500]


def human_comment_intro(
    *,
    status: str,
    stage: str,
    attempt: str,
    summary: str,
    next_action: str,
    failures: Optional[Sequence[Any]] = None,
) -> List[str]:
    status_labels = {
        "done": "PATBTAWO finished this task successfully.",
        "failed": "PATBTAWO could not complete this task.",
        "blocked": "PATBTAWO found an external blocker.",
        "retrying": "PATBTAWO hit a retryable issue and will try again.",
        "handoff": "PATBTAWO completed this stage and is moving to the next one.",
        "resumed": "PATBTAWO resumed an interrupted run.",
    }
    headline = status_labels.get(status, f"PATBTAWO update: {status}.")
    lines = [
        headline,
        f"Stage: {stage}. Attempt: {attempt}.",
    ]
    reason = summarize_failures(failures)
    if summary:
        clean_summary = summary[:500].strip()
        if reason and " because " not in clean_summary.lower():
            lines.append(f"Summary: {strip_sentence_end(clean_summary)} because {ensure_sentence(reason)}")
        else:
            lines.append(f"Summary: {ensure_sentence(clean_summary)}")
    elif reason:
        lines.append(f"Summary: This happened because {ensure_sentence(reason)}")
    if next_action:
        lines.append(f"Next: {ensure_sentence(next_action[:500])}")
    return lines


class TaskOrchestrator:
    def __init__(self, config: OrchestratorConfig, provider: Any) -> None:
        self.config = config
        self.provider = provider
        self.state = StateStore(config.artifact_root / "orchestrator_state.json")
        self.worktrees = WorktreeManager(config)
        self.runner = StageRunner(config)

    def run(self, *, once: bool = False) -> None:
        self.config.artifact_root.mkdir(parents=True, exist_ok=True)
        active = self.state.active()
        if active:
            self._resume_active(active)
            if once:
                return

        while True:
            task_stub = self.provider.get_next_ready_task(self.config.sections["ready"])
            if not task_stub:
                print("Ready queue is empty.")
                return

            task_id = str(task_stub["gid"])
            print(f"Selected Ready task {task_id}: {task_stub.get('name', '')}")
            task_contract = self._contract_for(task_id)
            result = self._process_task(task_contract, start_attempt=0)
            if once:
                return
            print(f"Task {task_id} finished with {result}; checking Ready for the next task.")

    def _resume_active(self, active: Mapping[str, Any]) -> None:
        task_id = str(active.get("task_id") or "")
        if not task_id:
            self.state.clear_active()
            return
        print(f"Found interrupted attempt for task {task_id}; restarting it from a fresh worktree.")
        old_worktrees = list(active.get("worktree_paths") or [])
        if active.get("worktree_path"):
            old_worktrees.append(str(active["worktree_path"]))
        for old_worktree in sorted(set(old_worktrees)):
            self.worktrees.remove(Path(str(old_worktree)))
        task_contract = active.get("task_contract")
        if not isinstance(task_contract, dict):
            task_contract = self._contract_for(task_id)
        attempt = parse_nonnegative_int(str(active.get("attempt", 1)), default=1)
        self._process_task(task_contract, start_attempt=max(0, attempt - 1), resumed=True)

    def _contract_for(self, task_id: str) -> Dict[str, Any]:
        task_contract = self.provider.get_task_contract(task_id)
        task_contract["orchestrator_contract"] = {
            "provider": self.config.provider_name,
            "project_gid": self.config.project_gid,
            "project_id": self.config.project_gid,
            "fetched_at": utc_now(),
            "source": self.config.provider_name,
            "ready_section_gid": self.config.sections["ready"],
            "ready_state_id": self.config.sections["ready"],
        }
        return task_contract

    def _process_task(
        self,
        task_contract: Mapping[str, Any],
        *,
        start_attempt: int,
        resumed: bool = False,
    ) -> str:
        task_id = str(task_contract["gid"])
        max_attempts = self.config.retry_limit + 1
        attempt = start_attempt
        if resumed:
            self._comment(
                task_id,
                status="resumed",
                stage="orchestrator",
                attempt=f"{min(attempt + 1, max_attempts)}/{max_attempts}",
                summary="Recovered an interrupted run; starting a fresh worktree for the task.",
                next_action="Continuing orchestration.",
            )

        while attempt < max_attempts:
            attempt += 1
            attempt_dir = self._attempt_artifact_dir(task_id, attempt)
            worktree_path, branch = self.worktrees.create(task_id, attempt, stage="builder")
            active = {
                "task_id": task_id,
                "task_name": task_contract.get("name"),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "branch": branch,
                "worktree_path": str(worktree_path),
                "worktree_paths": [str(worktree_path)],
                "artifact_dir": str(attempt_dir),
                "task_contract": dict(task_contract),
                "stage": "building",
                "started_at": utc_now(),
            }
            self.state.save_active(active)

            try:
                outcome = self._run_attempt(task_contract, attempt, attempt_dir, worktree_path)
            finally:
                active_after = self.state.active()
                if not active_after or active_after.get("task_id") != task_id:
                    self.worktrees.remove(worktree_path)

            if outcome.succeeded:
                self.state.clear_active()
                self.worktrees.remove(worktree_path)
                return "success"
            if outcome.blocked:
                self._move(task_id, "blocked")
                self._comment_outcome(task_id, outcome, attempt, max_attempts, terminal="blocked")
                self.state.clear_active()
                self.worktrees.remove(worktree_path)
                return "blocked"
            if outcome.retryable and attempt < max_attempts:
                self._comment_outcome(task_id, outcome, attempt, max_attempts, terminal="retrying")
                self.worktrees.remove(worktree_path)
                continue

            self._move(task_id, "failed")
            self._comment_outcome(task_id, outcome, attempt, max_attempts, terminal="failed")
            self.state.clear_active()
            self.worktrees.remove(worktree_path)
            return "failed"
        return "exhausted"

    def _append_active_worktree(self, path: Path) -> None:
        state = self.state.load()
        active = state.get("active_attempt") or {}
        paths = list(active.get("worktree_paths") or [])
        path_string = str(path)
        if path_string not in paths:
            paths.append(path_string)
        active["worktree_paths"] = paths
        active["updated_at"] = utc_now()
        state["active_attempt"] = active
        state["updated_at"] = utc_now()
        atomic_write_json(self.state.path, state)

    def _context_summary_payload(
        self,
        *,
        task_contract: Mapping[str, Any],
        attempt: int,
        build: StageOutcome,
        verify: StageOutcome,
        deploy: Optional[StageOutcome],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "task_id": str(task_contract["gid"]),
            "task_name": str(task_contract.get("name", "")),
            "task_notes": str(task_contract.get("notes") or task_contract.get("description") or ""),
            "task_url": str(task_contract.get("url") or task_contract.get("permalink_url") or ""),
            "attempt": attempt,
            "provider": self.config.provider_name,
            "project_gid": self.config.project_gid,
            "base_branch": self.config.base_branch,
            "context_path": str(self.config.context_path),
            "task_contract": dict(task_contract),
            "stages": {
                "builder": stage_report_summary(build),
                "verifier": stage_report_summary(verify),
            },
        }
        if deploy is not None:
            payload["stages"]["deployer"] = stage_report_summary(deploy)
        return payload

    def _run_attempt(
        self,
        task_contract: Mapping[str, Any],
        attempt: int,
        attempt_dir: Path,
        worktree_path: Path,
    ) -> StageOutcome:
        if self._is_planning_task(task_contract):
            return self._run_planning_attempt(task_contract, attempt, attempt_dir, worktree_path)

        task_id = str(task_contract["gid"])
        secondary_worktrees: List[Path] = []
        self._move(task_id, "building")
        try:
            build = self.runner.run(
                stage="builder",
                command=self.config.builder_command,
                task_contract=task_contract,
                attempt=attempt,
                worktree_path=worktree_path,
                attempt_artifact_dir=attempt_dir,
            )
            self.state.record_stage("builder", build)
            if not build.succeeded:
                return build
            self._comment_outcome(task_id, build, attempt, self.config.retry_limit + 1, terminal="handoff")

            try:
                verified_ref = self.worktrees.checkpoint(worktree_path, task_id=task_id, attempt=attempt, stage="builder")
            except CommandError as exc:
                return synthetic_stage_outcome(
                    stage="builder",
                    task_id=task_id,
                    attempt=attempt,
                    attempt_artifact_dir=attempt_dir,
                    summary="Builder changes could not be checkpointed for objective verification",
                    failures=[str(exc)],
                    next_action="Fix the builder worktree state so it can be committed before verification.",
                )

            self._move(task_id, "verifying")
            verifier_worktree = worktree_path
            if self.config.objective_verifier:
                verifier_worktree, _ = self.worktrees.create(
                    task_id,
                    attempt,
                    stage="verifier",
                    base_ref=verified_ref,
                )
                secondary_worktrees.append(verifier_worktree)
                self._append_active_worktree(verifier_worktree)

            verify = self.runner.run(
                stage="verifier",
                command=self.config.verifier_command,
                task_contract=task_contract,
                attempt=attempt,
                worktree_path=verifier_worktree,
                attempt_artifact_dir=attempt_dir,
            )
            self.state.record_stage("verifier", verify)
            if not verify.succeeded:
                return verify

            verifier_mutations = tracked_changed_files(verifier_worktree)
            if verifier_mutations:
                verify = synthetic_stage_outcome(
                    stage="verifier",
                    task_id=task_id,
                    attempt=attempt,
                    attempt_artifact_dir=attempt_dir,
                    summary="Verifier modified tracked files; objective verification must be read-only",
                    failures=[f"verifier changed tracked file: {path}" for path in verifier_mutations],
                    next_action="Update the verifier command so it reports findings without editing the worktree.",
                )
                self.state.record_stage("verifier", verify)
                return verify

            deploy_command = self.config.deploy_stage_command() if self.config.deploy_enabled else None
            deploy: Optional[StageOutcome] = None
            if deploy_command:
                self._comment_outcome(task_id, verify, attempt, self.config.retry_limit + 1, terminal="handoff")
                self._move(task_id, "deploying")
                deploy_worktree = verifier_worktree
                if self.config.objective_verifier:
                    deploy_worktree, _ = self.worktrees.create(
                        task_id,
                        attempt,
                        stage="deployer",
                        base_ref=verified_ref,
                    )
                    secondary_worktrees.append(deploy_worktree)
                    self._append_active_worktree(deploy_worktree)
                deploy = self.runner.run(
                    stage="deployer",
                    command=deploy_command,
                    task_contract=task_contract,
                    attempt=attempt,
                    worktree_path=deploy_worktree,
                    attempt_artifact_dir=attempt_dir,
                )
                self.state.record_stage("deployer", deploy)
                if not deploy.succeeded:
                    return deploy

            final_outcome = deploy or verify
            final_worktree = worktree_path
            if self.config.context_update_command:
                if deploy is not None:
                    self._comment_outcome(task_id, deploy, attempt, self.config.retry_limit + 1, terminal="handoff")
                else:
                    self._comment_outcome(task_id, verify, attempt, self.config.retry_limit + 1, terminal="handoff")

                context_worktree, _ = self.worktrees.create(
                    task_id,
                    attempt,
                    stage="context",
                    base_ref=verified_ref,
                )
                secondary_worktrees.append(context_worktree)
                self._append_active_worktree(context_worktree)

                context_summary_path = attempt_dir / "context" / "task_summary.json"
                context_summary_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(
                    context_summary_path,
                    self._context_summary_payload(
                        task_contract=task_contract,
                        attempt=attempt,
                        build=build,
                        verify=verify,
                        deploy=deploy,
                    ),
                )
                context = self.runner.run(
                    stage="context",
                    command=self.config.context_update_command,
                    task_contract=task_contract,
                    attempt=attempt,
                    worktree_path=context_worktree,
                    attempt_artifact_dir=attempt_dir,
                    extra_env={
                        "ORCHESTRATOR_TASK_SUMMARY_PATH": str(context_summary_path),
                    },
                )
                self.state.record_stage("context", context)
                if not context.succeeded:
                    return context

                expected_context_file = self.config.context_path.relative_to(self.config.repo_root).as_posix()
                context_mutations = [Path(path).as_posix() for path in tracked_changed_files(context_worktree)]
                unexpected_context_changes = [path for path in context_mutations if path != expected_context_file]
                if not context_mutations or unexpected_context_changes:
                    context = synthetic_stage_outcome(
                        stage="context",
                        task_id=task_id,
                        attempt=attempt,
                        attempt_artifact_dir=attempt_dir,
                        summary="Context updater did not only update the shared context file",
                        failures=(
                            [f"expected context file change: {expected_context_file}"]
                            if not context_mutations
                            else [f"unexpected tracked file changed: {path}" for path in unexpected_context_changes]
                        ),
                        next_action="Update the context updater command so it only edits the shared context file.",
                    )
                    self.state.record_stage("context", context)
                    return context

                try:
                    self.worktrees.checkpoint(context_worktree, task_id=task_id, attempt=attempt, stage="context")
                except CommandError as exc:
                    return synthetic_stage_outcome(
                        stage="context",
                        task_id=task_id,
                        attempt=attempt,
                        attempt_artifact_dir=attempt_dir,
                        summary="Context changes could not be checkpointed for merge",
                        failures=[str(exc)],
                        next_action="Fix the context updater worktree state so it can be committed before merge.",
                    )

                final_outcome = context
                final_worktree = context_worktree

            try:
                self.worktrees.merge_into_base(self.worktrees.current_commit(final_worktree))
            except CommandError as exc:
                return synthetic_stage_outcome(
                    stage="orchestrator",
                    task_id=task_id,
                    attempt=attempt,
                    attempt_artifact_dir=attempt_dir,
                    summary="Task changes could not be merged back into the base branch",
                    failures=[str(exc)],
                    next_action="Fix the base branch or rerun the orchestrator from a named base branch.",
                )
            self._move(task_id, "done")
            self._comment_outcome(task_id, final_outcome, attempt, self.config.retry_limit + 1, terminal="done")
            final_outcome.status = "success"
            return final_outcome
        finally:
            for secondary_worktree in reversed(secondary_worktrees):
                self.worktrees.remove(secondary_worktree)

    def _is_planning_task(self, task_contract: Mapping[str, Any]) -> bool:
        return bool(
            self.config.planner_command
            and task_has_planner_signal(task_contract, self.config.planner_tag)
        )

    def _run_planning_attempt(
        self,
        task_contract: Mapping[str, Any],
        attempt: int,
        attempt_dir: Path,
        worktree_path: Path,
    ) -> StageOutcome:
        task_id = str(task_contract["gid"])
        if not self.config.planner_command:
            return synthetic_stage_outcome(
                stage="planner",
                task_id=task_id,
                attempt=attempt,
                attempt_artifact_dir=attempt_dir,
                summary="Planning task cannot run because no planner command is configured",
                failures=["ORCHESTRATOR_PLANNER_AGENT_COMMAND is not set"],
                next_action="Configure a planner command or remove the planner signal from this task.",
            )

        self._move(task_id, "building")
        plan_path = attempt_dir / "planner" / "task_plan.json"
        planner = self.runner.run(
            stage="planner",
            command=self.config.planner_command,
            task_contract=task_contract,
            attempt=attempt,
            worktree_path=worktree_path,
            attempt_artifact_dir=attempt_dir,
            extra_env={"ORCHESTRATOR_TASK_PLAN_PATH": str(plan_path)},
        )
        self.state.record_stage("planner", planner)
        if not planner.succeeded:
            return planner

        planned_tasks, plan_failures = load_task_plan(planner, plan_path)
        if plan_failures:
            planner = synthetic_stage_outcome(
                stage="planner",
                task_id=task_id,
                attempt=attempt,
                attempt_artifact_dir=attempt_dir,
                summary="Planner did not produce a valid task plan",
                failures=plan_failures,
                next_action="Fix the planner output so it contains a tasks array with title and description.",
            )
            self.state.record_stage("planner", planner)
            return planner

        created, creation_failures = self._create_planned_tasks(task_contract, planned_tasks)
        if creation_failures:
            planner = synthetic_stage_outcome(
                stage="planner",
                task_id=task_id,
                attempt=attempt,
                attempt_artifact_dir=attempt_dir,
                summary="Planner task creation failed",
                failures=creation_failures,
                next_action="Inspect provider task creation permissions and rerun the planner task.",
                retryable=True,
            )
            self.state.record_stage("planner", planner)
            return planner

        planner.report["created_tasks"] = created
        planner.report["summary"] = f"Planner created {len(created)} task(s)."
        planner.report["next_action"] = created_tasks_next_action(created)
        planner.summary = str(planner.report["summary"])
        atomic_write_json(planner.report_path, planner.report)
        self._move(task_id, "done")
        self._comment_outcome(task_id, planner, attempt, self.config.retry_limit + 1, terminal="done")
        return planner

    def _create_planned_tasks(
        self,
        source_task: Mapping[str, Any],
        planned_tasks: Sequence[Mapping[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        create_task = getattr(self.provider, "create_task", None)
        if not callable(create_task):
            return [], [f"{self.config.provider_name} provider does not support task creation"]

        created: List[Dict[str, Any]] = []
        failures: List[str] = []
        for index, item in enumerate(planned_tasks, start=1):
            title = str(item["title"])
            description = plan_task_description(item, source_task=source_task)
            tags = normalize_plan_tags(item.get("tags"))
            try:
                task = create_task(
                    title=title,
                    description=description,
                    ready_state=self.config.sections["ready"],
                    tags=tags,
                    parent_task_id=str(source_task.get("gid") or ""),
                )
            except ProviderError as exc:
                failures.append(f"task {index} ({title}) could not be created: {exc}")
                continue
            created.append(
                {
                    "id": str(task.get("gid") or task.get("id") or ""),
                    "title": str(task.get("name") or title),
                    "url": str(task.get("permalink_url") or task.get("url") or ""),
                    "tags": tags,
                }
            )
        return created, failures

    def _attempt_artifact_dir(self, task_id: str, attempt: int) -> Path:
        safe_task = slug(task_id)
        return self.config.artifact_root / safe_task / f"attempt-{attempt:03d}-{compact_timestamp()}"

    def _move(self, task_id: str, section: str) -> None:
        print(f"Moving task {task_id} to {section}")
        self.provider.move_task(task_id, self.config.sections[section])

    def _comment_outcome(
        self,
        task_id: str,
        outcome: StageOutcome,
        attempt: int,
        max_attempts: int,
        *,
        terminal: str,
    ) -> None:
        next_action = str(outcome.report.get("next_action", ""))
        if terminal == "handoff":
            status = "handoff"
            next_action = next_action or "Proceeding to the next lifecycle section."
        elif terminal == "retrying":
            status = "retrying"
            next_action = next_action or "Retrying transient failure with a fresh worktree."
        else:
            status = terminal
        self._comment(
            task_id,
            status=status,
            stage=outcome.stage,
            attempt=f"{attempt}/{max_attempts}",
            summary=outcome.summary,
            next_action=next_action,
            failures=outcome.report.get("failures") if isinstance(outcome.report.get("failures"), list) else None,
            artifacts=[str(outcome.report_path), str(outcome.log_path)],
        )

    def _comment(
        self,
        task_id: str,
        *,
        status: str,
        stage: str,
        attempt: str,
        summary: str,
        next_action: str,
        failures: Optional[Sequence[Any]] = None,
        artifacts: Optional[Sequence[str]] = None,
    ) -> None:
        human_lines = human_comment_intro(
            status=status,
            stage=stage,
            attempt=attempt,
            summary=summary,
            next_action=next_action,
            failures=failures,
        )
        lines = [
            *human_lines,
            "",
            "[orchestrator]",
            f"status: {status}",
            f"stage: {stage}",
            f"attempt: {attempt}",
            f"summary: {summary[:500]}",
            f"next: {next_action[:500]}",
        ]
        if artifacts:
            lines.append("artifacts: " + ", ".join(artifacts[:3]))
        self.provider.comment(task_id, "\n".join(lines))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the provider-agnostic task workflow orchestrator.")
    parser.add_argument("--env-file", type=Path, help="Optional KEY=VALUE env file to load before config.")
    parser.add_argument(
        "--provider",
        choices=["asana", "trello", "clickup", "jira", "monday", "command"],
        help="Task provider override. Defaults to ORCHESTRATOR_PROVIDER or asana.",
    )
    parser.add_argument("--once", action="store_true", help="Process at most one task, then exit.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip provider mutations but still run local stages. With --reset-attempts, print cleanup actions only.",
    )
    parser.add_argument("--validate-config", action="store_true", help="Validate configuration and exit.")
    parser.add_argument("--print-config", action="store_true", help="Print redacted resolved configuration.")
    parser.add_argument(
        "--reset-attempts",
        action="store_true",
        help="Clear local attempt state, attempt artifacts, attempt worktrees, and temporary attempt branches, then exit.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    if args.env_file:
        load_env_file(args.env_file, environ)
    elif Path(".env").exists():
        load_env_file(Path(".env"), environ)

    if args.reset_attempts:
        config = attempt_reset_config_from_env(environ, cwd=Path.cwd())
        counts = reset_attempts(config, dry_run=bool(args.dry_run))
        return 1 if counts.get("failures") else 0

    provider_name = normalize_provider_name(args.provider or environ.get("ORCHESTRATOR_PROVIDER"))
    try:
        config = OrchestratorConfig.from_env(
            environ,
            cwd=Path.cwd(),
            dry_run_override=bool(args.dry_run),
            provider_override=args.provider,
        )
        if args.print_config:
            print(json.dumps(config.redacted_dict(), indent=2, sort_keys=True))
        validate_runtime_config(config)
        if args.validate_config:
            print("Configuration OK.")
            return 0
        provider = build_provider(config)
        TaskOrchestrator(config, provider).run(once=bool(args.once))
        return 0
    except MissingConfigError as exc:
        print(f"Configuration error: {configuration_help(provider_name, exc)}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (ProviderError, CommandError) as exc:
        print(f"Orchestrator error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
