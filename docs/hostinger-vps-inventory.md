# Hostinger VPS Inventory

Task: `86d328xjm` / P0 - Inspect Hostinger VPS specs and services

Captured: 2026-05-20 11:24 UTC from the builder worktree.

## Target

- Host: `pogojar.com`
- Resolved IPv4: `157.173.220.77`
- Intended public app host from orchestrator environment:
  `pogojar.com`
- Intended public app URL from orchestrator environment:
  `https://pogojar.com`
- Intended deploy path from orchestrator environment:
  `~/domains/pogojar.com/public_html`

## Confirmed Public Observations

| Check | Result |
| --- | --- |
| DNS `pogojar.com` | `A 157.173.220.77` |
| Demo deployment surface | Root domain `pogojar.com` only; no subdomain is used |
| TCP 22 | Open |
| TCP 80 | Open |
| TCP 443 | Open |
| TCP 3001 | Open |
| TCP 5432 | Closed / unreachable from builder |
| TCP 3000 | Closed / unreachable from builder |
| TCP 5000 | Closed / unreachable from builder |
| TCP 8000 | Closed / unreachable from builder |
| TCP 8080 | Closed / unreachable from builder |
| TCP 8443 | Closed / unreachable from builder |
| HTTP `http://pogojar.com/` | `301` redirect to `https://pogojar.com/` |
| HTTP server header | `nginx/1.18.0 (Ubuntu)` |
| SSH banner | `SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.15` |
| HTTP `http://pogojar.com:3001/` | `200`, `X-Powered-By: Next.js` |

## Current Inventory Status

| Item | Status |
| --- | --- |
| OS version | Not confirmed from `/etc/os-release`; public SSH and Nginx banners indicate Ubuntu packages. OpenSSH `8.9p1 Ubuntu-3ubuntu0.15` is consistent with Ubuntu 22.04 LTS, but this needs confirmation on-host. |
| CPU | Not available from public checks. Requires Hostinger API details or on-host `lscpu`. |
| RAM | Not available from public checks. Requires Hostinger API details or on-host `free -h`. |
| Disk | Not available from public checks. Requires Hostinger API details or on-host `df -hT` / `lsblk`. |
| Open ports | Public reachability recorded above. Full listening socket inventory requires on-host `ss -tulpen` or `netstat -tulpen`. |
| Nginx | Public HTTP header confirms Nginx is serving port 80: `nginx/1.18.0 (Ubuntu)`. |
| Node / app service | Port `3001` serves a Next.js app directly and returns `X-Powered-By: Next.js`. |
| PM2 | Not confirmable without shell or Hostinger project/container data. |
| PostgreSQL | Public TCP `5432` is not reachable from builder. On-host process/service check still required. |
| Docker Compose projects | Not confirmable because the read-only Hostinger connector call was unavailable. |

## Access Attempts

- Read-only Hostinger connector call `VPS_getVirtualMachinesV1` was attempted
  twice and returned `user cancelled MCP tool call`.
- SSH using the configured deploy user and key reached `pogojar.com` but was
  rejected with `Permission denied (publickey,password)`.
- SSH as `root` with the configured key could not load the key under the
  sandbox user ACL and then failed authentication.
- No install, package, service restart, firewall, or file mutation commands were
  run against the VPS.

## SSH Hardening Follow-Up

The deploy-user provisioning and SSH hardening runbook is documented in
[hostinger-ssh-hardening.md](hostinger-ssh-hardening.md). It uses
`scripts/provision_deploy_user_harden_ssh.sh` to create the non-root deploy user
first, confirm key login from a second terminal, and only then disable
password-based SSH authentication.

## Base Package Follow-Up

The base package installation runbook is documented in
[hostinger-base-vps-packages.md](hostinger-base-vps-packages.md). It uses
`scripts/install_base_vps_packages.sh` to install and verify `curl`, `git`,
`build-essential`, `nginx`, `ufw`, `unzip`, and `ca-certificates`, then prints
package versions plus nginx/ufw service status for the task record.

## Node.js And PM2 Follow-Up

The Node.js LTS and PM2 runbook is documented in
[hostinger-node-pm2.md](hostinger-node-pm2.md). It uses
`scripts/install_node_pm2.sh` to install the current Node.js LTS line, verify
that `npm` is available, install PM2 globally, and enable the `pm2-deploy`
systemd startup service so saved PM2 apps restart after reboot.

## Read-Only On-Host Audit Command

When SSH or Hostinger API access is available, run the repository script below
from a trusted workstation and save the output into this document or a sibling
artifact:

```sh
ssh -i "$ORCHESTRATOR_DEPLOY_SSH_KEY_PATH" -l root pogojar.com 'sh -s' \
  < scripts/audit_hostinger_vps_readonly.sh
```

If Hostinger provides a non-root SSH user, replace `root` with that account.
The script only runs read-only inspection commands.
