"""Test connectivity to the production server.

Reads ORCHESTRATOR_DEPLOY_* env vars (set by the orchestrator deployer stage
adapter). Tests:

  1. DNS resolution of the production host.
  2. HTTPS connectivity to the production URL.
  3. SSH connectivity with the configured deploy key.

Exits with 0 if all relevant checks pass, non-zero otherwise.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from urllib import error, request


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def fail(message: str) -> int:
    print(f"test-connection: {message}", file=sys.stderr)
    return 1


def check_dns(host: str) -> tuple[bool, str]:
    try:
        addrs = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted(set(addr[4][0] for addr in addrs))
        return True, f"DNS resolved {host} -> {', '.join(ips)}"
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for {host}: {exc}"


def check_https(url: str, timeout: int = 15) -> tuple[bool, str, int | None]:
    started = time.monotonic()
    try:
        req = request.Request(url, headers={"User-Agent": "PATBTAWO test-connection"})
        with request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
        elapsed = time.monotonic() - started
        ok = 200 <= status < 400
        if ok:
            return True, f"HTTPS {status} from {url} in {elapsed:.1f}s", status
        return False, f"HTTPS {status} from {url} (expected 2xx/3xx)", status
    except error.HTTPError as exc:
        elapsed = time.monotonic() - started
        return False, f"HTTPS {exc.code} {exc.reason} from {url} in {elapsed:.1f}s", exc.code
    except Exception as exc:
        elapsed = time.monotonic() - started
        return False, f"HTTPS connection failed to {url} in {elapsed:.1f}s: {exc}", None


def check_ssh(
    user: str, host: str, port: int = 22, key_path: str = "", timeout: int = 10,
) -> tuple[bool, str]:
    ssh_args = ["ssh"]
    if key_path:
        ssh_args.extend(["-i", key_path])
    ssh_args.extend([
        "-o", f"ConnectTimeout={timeout}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(port),
        f"{user}@{host}",
        "echo ok",
    ])
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ssh_args,
            capture_output=True, text=True, timeout=timeout + 5,
        )
        elapsed = time.monotonic() - started
        if completed.returncode == 0:
            return True, f"SSH {user}@{host}:{port} connected in {elapsed:.1f}s"
        err = (completed.stderr or "").strip().split("\n")[-1] if completed.stderr else f"exit code {completed.returncode}"
        return False, f"SSH {user}@{host}:{port} failed ({err})"
    except subprocess.TimeoutExpired:
        return False, f"SSH {user}@{host}:{port} timed out after {timeout}s"
    except FileNotFoundError:
        return False, "ssh command not found in PATH"
    except Exception as exc:
        return False, f"SSH {user}@{host}:{port} error: {exc}"


def main() -> int:
    host = env("ORCHESTRATOR_DEPLOY_HOST")
    user = env("ORCHESTRATOR_DEPLOY_USER")
    key_path = env("ORCHESTRATOR_DEPLOY_SSH_KEY_PATH")
    ssh_port_str = env("ORCHESTRATOR_DEPLOY_SSH_PORT", "22")
    smoke_url_val = env("ORCHESTRATOR_SMOKE_URL", "")
    deploy_url = env("ORCHESTRATOR_DEPLOY_URL", "")

    checks: list[dict] = []
    failures: list[str] = []

    # Determine the URLs to test
    https_targets: list[str] = []
    if smoke_url_val:
        https_targets.append(smoke_url_val)
    if deploy_url and deploy_url != smoke_url_val:
        https_targets.append(deploy_url)
    if not https_targets:
        if host:
            https_targets.append(f"https://{host}/")
        https_targets.append("https://pogojar.com/")

    # Determine the SSH target
    ssh_host = host or "pogojar.com"
    if not user:
        user = env("ORCHESTRATOR_DEPLOY_USER", "")

    try:
        ssh_port = int(ssh_port_str)
    except ValueError:
        ssh_port = 22

    # 1. DNS check
    resolved_host = host or ssh_host
    dns_ok, dns_msg = check_dns(resolved_host)
    checks.append({
        "name": f"dns {resolved_host}",
        "status": "passed" if dns_ok else "failed",
        "message": dns_msg,
    })
    if not dns_ok:
        failures.append(dns_msg)

    # 2. HTTPS checks
    for url in https_targets:
        https_ok, https_msg, status_code = check_https(url)
        entry: dict = {
            "name": f"https {url}",
            "status": "passed" if https_ok else "failed",
            "message": https_msg,
        }
        if status_code is not None:
            entry["status_code"] = status_code
        checks.append(entry)
        if not https_ok:
            failures.append(https_msg)

    # 3. SSH check (only if deploy user is configured)
    if user:
        ssh_ok, ssh_msg = check_ssh(user, ssh_host, ssh_port, key_path)
        checks.append({
            "name": f"ssh {user}@{ssh_host}:{ssh_port}",
            "status": "passed" if ssh_ok else "failed",
            "message": ssh_msg,
        })
        if not ssh_ok:
            failures.append(ssh_msg)
    else:
        checks.append({
            "name": "ssh",
            "status": "skipped",
            "message": "ORCHESTRATOR_DEPLOY_USER is not set; SSH check skipped",
        })

    # --- Report ---
    report = {
        "task_id": env("ORCHESTRATOR_TASK_ID", "unknown"),
        "status": "success" if not failures else "failed",
        "summary": "All connection checks passed." if not failures else f"{len(failures)} check(s) failed.",
        "changed_files": [],
        "checks": checks,
        "failures": failures,
        "next_action": "Configure deploy variables and re-run if SSH/HTTPS checks failed.",
        "artifact_paths": [],
        "stage": "builder",
    }
    print(json.dumps(report, indent=2))

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
