#!/usr/bin/env python3
"""Provider-agnostic task workflow orchestrator.

The orchestrator intentionally keeps its dependencies to Python's standard
library so it can be dropped into small repositories without adding package
management first. Builder/verifier/deployer commands are supplied by config and
must write JSON reports to the path provided in ORCHESTRATOR_REPORT_PATH.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib import error, parse, request


ASANA_API_BASE = "https://app.asana.com/api/1.0"
TRELLO_API_BASE = "https://api.trello.com/1"
CLICKUP_API_BASE = "https://api.clickup.com/api/v2"
MONDAY_API_BASE = "https://api.monday.com/v2"
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
SUCCESS_STATUSES = {"success", "succeeded", "pass", "passed", "ok", "done"}
FAILED_STATUSES = {"failed", "failure", "error", "errored"}
BLOCKED_STATUSES = {"blocked", "blocker", "external_blocker"}
TRANSIENT_EXIT_CODES = {75}


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
    worktree_root: Path
    artifact_root: Path
    builder_command: str
    verifier_command: str
    deploy_command: Optional[str]
    smoke_command: Optional[str]
    deploy_enabled: bool
    objective_verifier: bool
    keep_worktrees: bool
    stage_timeout_seconds: Optional[int]

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
        artifact_root = Path(
            environ.get("ORCHESTRATOR_ARTIFACT_ROOT") or repo_root / ".orchestrator" / "artifacts"
        ).expanduser()
        if not worktree_root.is_absolute():
            worktree_root = (repo_root / worktree_root).resolve()
        if not artifact_root.is_absolute():
            artifact_root = (repo_root / artifact_root).resolve()

        deploy_command = blank_to_none(
            first_env(environ, ["ORCHESTRATOR_DEPLOYER_AGENT_COMMAND", "ORCHESTRATOR_DEPLOY_COMMAND"])
        )
        smoke_command = blank_to_none(environ.get("ORCHESTRATOR_SMOKE_COMMAND"))
        deploy_enabled = truthy(environ.get("ORCHESTRATOR_DEPLOY_ENABLED"))
        if environ.get("ORCHESTRATOR_DEPLOY_ENABLED") is None:
            deploy_enabled = bool(deploy_command or smoke_command)
        if deploy_enabled and not (deploy_command or smoke_command):
            raise ConfigError(
                "ORCHESTRATOR_DEPLOY_ENABLED is true but neither "
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
            worktree_root=worktree_root.resolve(),
            artifact_root=artifact_root.resolve(),
            builder_command=builder_command,
            verifier_command=verifier_command,
            deploy_command=deploy_command,
            smoke_command=smoke_command,
            deploy_enabled=deploy_enabled,
            objective_verifier=not falsey(environ.get("ORCHESTRATOR_OBJECTIVE_VERIFIER")),
            keep_worktrees=truthy(environ.get("ORCHESTRATOR_KEEP_WORKTREES")),
            stage_timeout_seconds=stage_timeout,
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
        for key in ("repo_root", "worktree_root", "artifact_root"):
            payload[key] = str(payload[key])
        return payload


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
        return required_env(
            environ,
            [
                "ORCHESTRATOR_COMMAND_NEXT_TASK",
                "ORCHESTRATOR_COMMAND_GET_TASK",
                "ORCHESTRATOR_COMMAND_MOVE_TASK",
                "ORCHESTRATOR_COMMAND_COMMENT_TASK",
            ],
        )
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
    def __init__(self, token: str, *, dry_run: bool = False, api_base: str = ASANA_API_BASE) -> None:
        self.token = token
        self.dry_run = dry_run
        self.api_base = api_base.rstrip("/")

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        response = self._request(
            "GET",
            f"/sections/{section_gid}/tasks",
            params={
                "limit": "1",
                "opt_fields": "gid,name,resource_type,modified_at,permalink_url",
            },
        )
        tasks = response.get("data") or []
        return tasks[0] if tasks else None

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
            params=self._params({"limit": 1, "fields": "id,name,desc,url,idList,dateLastActivity"}),
        )
        if not cards:
            return None
        card = cards[0]
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


class ClickUpProvider:
    def __init__(self, access_token: str, list_id: str, *, dry_run: bool = False, api_base: str = CLICKUP_API_BASE) -> None:
        self.access_token = access_token
        self.list_id = list_id
        self.dry_run = dry_run
        self.api_base = api_base.rstrip("/")
        self.http = HttpJsonClient()
        self._status_aliases: Optional[Dict[str, str]] = None

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": self.access_token}

    def get_next_ready_task(self, section_gid: str) -> Optional[Dict[str, Any]]:
        ready_values = self._status_match_values(section_gid)
        status_filter = self._status_api_value(section_gid)
        if self._looks_like_status_id(section_gid) and status_filter.strip().lower() == section_gid.strip().lower():
            status_filter = ""
        for page in range(20):
            params: Dict[str, Any] = {
                "archived": "false",
                "include_closed": "true",
                "subtasks": "true",
                "page": page,
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
                    return self._normalize(task)
            if not tasks:
                return None
        return None

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
            jql = f'project = "{project_key}" AND status = "{section_gid}" ORDER BY created ASC'
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
      limit: 1,
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
        completed = subprocess.run(
            command,
            shell=True,
            text=True,
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


class WorktreeManager:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

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
        return run_local(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    def remove(self, path: Path) -> None:
        ensure_under(path, self.config.worktree_root)
        if self.config.keep_worktrees:
            print(f"Keeping worktree due to ORCHESTRATOR_KEEP_WORKTREES: {path}")
            return
        if not path.exists():
            return
        print(f"Removing worktree {path}")
        run_local(["git", "worktree", "remove", "--force", str(path)], cwd=self.config.repo_root, check=False)
        if path.exists():
            shutil.rmtree(path)
        run_local(["git", "worktree", "prune"], cwd=self.config.repo_root, check=False)


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
    ) -> StageOutcome:
        stage_dir = attempt_artifact_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        report_path = stage_dir / f"{stage}_report.json"
        log_path = stage_dir / f"{stage}.log"
        contract_path = stage_dir / "task_contract.json"
        atomic_write_json(contract_path, task_contract)

        task_id = str(task_contract["gid"])
        subagent_id = f"{slug(task_id)}-{attempt}-{slug(stage)}-{compact_timestamp()}"
        env = os.environ.copy()
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
                "ORCHESTRATOR_ARTIFACT_DIR": str(stage_dir),
                "ORCHESTRATOR_ATTEMPT": str(attempt),
                "ORCHESTRATOR_PROJECT_GID": self.config.project_gid,
                "ORCHESTRATOR_PROJECT_ID": self.config.project_gid,
                "ORCHESTRATOR_PROVIDER": self.config.provider_name,
                "ORCHESTRATOR_STAGE_WORKTREE_PATH": str(worktree_path),
                "ORCHESTRATOR_OBJECTIVE_VERIFIER": "true" if self.config.objective_verifier else "false",
            }
        )

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
        try:
            completed = subprocess.run(
                command,
                cwd=str(worktree_path),
                env=env,
                shell=True,
                text=True,
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

        command_failed = exit_code not in (0, None)
        if timed_out:
            validation_failures.append("stage command timed out")
        if exit_code not in (0, None) and normalize_status(report.get("status")) == "success":
            validation_failures.append("stage command exited non-zero while report status was success")
        if exit_code == 0 and normalize_status(report.get("status")) == "unknown":
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

        status = normalize_status(report.get("status"))
        retryable = bool(report.get("retryable") or report.get("transient") or exit_code in TRANSIENT_EXIT_CODES)
        if command_failed and status != "blocked":
            status = "failed"
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
        "generated_by": "asana_orchestrator",
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
            self._process_task(task_contract, start_attempt=0)
            if once:
                return

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
    ) -> None:
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
                return
            if outcome.blocked:
                self._move(task_id, "blocked")
                self._comment_outcome(task_id, outcome, attempt, max_attempts, terminal="blocked")
                self.state.clear_active()
                self.worktrees.remove(worktree_path)
                return
            if outcome.retryable and attempt < max_attempts:
                self._comment_outcome(task_id, outcome, attempt, max_attempts, terminal="retrying")
                self.worktrees.remove(worktree_path)
                continue

            self._move(task_id, "failed")
            self._comment_outcome(task_id, outcome, attempt, max_attempts, terminal="failed")
            self.state.clear_active()
            self.worktrees.remove(worktree_path)
            return

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

    def _run_attempt(
        self,
        task_contract: Mapping[str, Any],
        attempt: int,
        attempt_dir: Path,
        worktree_path: Path,
    ) -> StageOutcome:
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
                self._move(task_id, "done")
                self._comment_outcome(task_id, deploy, attempt, self.config.retry_limit + 1, terminal="done")
                deploy.status = "success"
                return deploy

            self._move(task_id, "done")
            self._comment_outcome(task_id, verify, attempt, self.config.retry_limit + 1, terminal="done")
            verify.status = "success"
            return verify
        finally:
            for secondary_worktree in reversed(secondary_worktrees):
                self.worktrees.remove(secondary_worktree)

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
    parser.add_argument("--dry-run", action="store_true", help="Skip provider mutations but still run local stages.")
    parser.add_argument("--validate-config", action="store_true", help="Validate configuration and exit.")
    parser.add_argument("--print-config", action="store_true", help="Print redacted resolved configuration.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    if args.env_file:
        load_env_file(args.env_file, environ)
    elif Path(".env").exists():
        load_env_file(Path(".env"), environ)

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
