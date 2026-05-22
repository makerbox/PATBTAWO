# Hostinger Base VPS Packages Runbook

Task: `86d328y5h` / P0 - Install base VPS packages

Use this runbook from a trusted workstation with root or equivalent sudo access
to the VPS. It installs the minimum Ubuntu/Debian packages needed for app
hosting and deployment, then prints package versions and service status for the
task record.

## Target

- Host: `pogojar.com`
- Script: `scripts/install_base_vps_packages.sh`
- Required packages: `curl`, `git`, `build-essential`, `nginx`, `ufw`, `unzip`,
  `ca-certificates`

## Safety Model

- The script is idempotent and uses `apt-get install -y` for the required
  package list.
- Nginx is enabled and started by default when systemd is available. Set
  `START_NGINX=no` to leave the nginx service state unchanged.
- UFW is installed but not enabled by this task. Enabling the firewall belongs
  with a later firewall rule task so SSH is not locked out accidentally.
- The script fails if any required package or expected command is missing after
  installation.

## Install

Run over SSH as root:

```sh
ssh root@pogojar.com 'sh -s -- install' \
  < scripts/install_base_vps_packages.sh
```

If using a sudo-capable deploy user:

```sh
ssh deploy@pogojar.com 'sudo sh -s -- install' \
  < scripts/install_base_vps_packages.sh
```

## Verify And Document

Capture verification output after installation:

```sh
ssh root@pogojar.com 'sh -s -- verify' \
  < scripts/install_base_vps_packages.sh
```

The verification output documents:

| Item | Evidence |
| --- | --- |
| Package installation | `dpkg-query` status and package version for each required package |
| Build toolchain | paths and first-line versions for `gcc`, `g++`, and `make` |
| Deployment tools | paths and versions for `curl`, `git`, and `unzip` |
| Web server | nginx package version, `nginx -v`, service enabled/active state, and `nginx -t` |
| Firewall package | ufw package version, service enabled/active state, and `ufw status verbose` |
| CA certificates | package version and `update-ca-certificates` command path |

Expected package status for every required package:

```text
installed
```

Expected nginx service state on a systemd VPS:

```text
nginx active           active
nginx enabled          enabled
```

Expected UFW behavior for this task:

```text
ufw installed, with firewall activation deferred to the firewall configuration task
```

## Rollback

This task installs common base packages that are safe to leave in place. If a
package must be removed during testing, use `apt-get remove` for only that
package and re-run the verification command above before continuing.
