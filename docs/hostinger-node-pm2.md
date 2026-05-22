# Hostinger Node.js LTS And PM2 Runbook

Task: `86d328ym2` / P0 - Install Node.js LTS and PM2

Use this runbook from a trusted workstation with root or equivalent sudo access
to the VPS. It installs the current Node.js LTS line, installs PM2 globally with
npm, and configures PM2's systemd startup service for the deploy user so saved
apps restart after reboot.

## Target

- Host: `pogojar.com`
- Deploy user: `deploy`
- Script: `scripts/install_node_pm2.sh`
- Default Node.js LTS major: `24`
- Default PM2 systemd service: `pm2-deploy`

## Safety Model

- The script installs Node.js from the official Node.js binary tarball published
  under `https://nodejs.org/dist/latest-v24.x/`.
- The downloaded tarball is verified against Node.js `SHASUMS256.txt` before it
  is extracted.
- Node is installed under `/usr/local/lib/nodejs`, with command symlinks in
  `/usr/local/bin`.
- PM2 is installed globally by the installed npm runtime.
- PM2 startup is configured for `PM2_SERVICE_USER`, which defaults to the
  deploy user. The user must already exist.
- The script runs `pm2 save --force` for the service user so the PM2 systemd
  service has a saved process list to restore after reboot. Future deployments
  must run `pm2 save` again after starting or changing app processes.

## Prerequisite

Create the deploy user first with the SSH hardening runbook:

```sh
ssh root@pogojar.com \
  "DEPLOY_USER='deploy' DEPLOY_PUBLIC_KEY='$DEPLOY_PUBLIC_KEY' sh -s -- prepare" \
  < scripts/provision_deploy_user_harden_ssh.sh
```

## Install

Run over SSH as root:

```sh
ssh root@pogojar.com "PM2_SERVICE_USER='deploy' sh -s -- install" \
  < scripts/install_node_pm2.sh
```

If using a sudo-capable deploy user:

```sh
ssh deploy@pogojar.com "sudo PM2_SERVICE_USER='deploy' sh -s -- install" \
  < scripts/install_node_pm2.sh
```

Override `NODE_LTS_MAJOR` only when the official Node.js LTS line changes:

```sh
ssh root@pogojar.com "NODE_LTS_MAJOR='24' PM2_SERVICE_USER='deploy' sh -s -- install" \
  < scripts/install_node_pm2.sh
```

## Verify And Document

Capture verification output after installation:

```sh
ssh root@pogojar.com "PM2_SERVICE_USER='deploy' sh -s -- verify" \
  < scripts/install_node_pm2.sh
```

The verification output documents:

| Item | Evidence |
| --- | --- |
| Node.js LTS | `node --version` starts with `v24.` |
| npm availability | `npm --version` returns a version |
| PM2 global install | `pm2 --version` and `npm list -g --depth=0 pm2` pass |
| PM2 startup | `systemctl is-enabled pm2-deploy` reports `enabled` |
| PM2 saved process list | `/home/deploy/.pm2/dump.pm2` exists |

Expected service state on a systemd VPS:

```text
pm2-deploy enabled      enabled
```

After an app is started or reloaded with PM2, save the updated process list:

```sh
ssh deploy@pogojar.com 'pm2 save'
```

## Rollback

If startup must be removed during testing:

```sh
ssh root@pogojar.com \
  "env PATH=/usr/local/bin:\$PATH pm2 unstartup systemd -u deploy --hp /home/deploy || true"
```

To remove PM2 while leaving Node.js in place:

```sh
ssh root@pogojar.com \
  "env PATH=/usr/local/bin:\$PATH npm uninstall -g pm2 && rm -f /usr/local/bin/pm2"
```

Leave Node.js installed unless there is a specific version conflict. Production
deployments should use an active or maintenance LTS release line.
