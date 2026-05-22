# Hostinger SSH Hardening Runbook

Task: `86d328y0w` / P0 - Create deploy user and harden SSH

Use this runbook from a trusted workstation with current root or provider-console
access to the VPS. Keep the original root SSH session open until the final deploy
user login has been tested after hardening.

## Target

- Host: `pogojar.com`
- Deploy user: `deploy`
- Script: `scripts/provision_deploy_user_harden_ssh.sh`

## Safety Model

- The provisioning script is two-phase by default.
- `prepare` creates or updates the non-root deploy user, installs the public key,
  locks password login for that user, and leaves global sshd authentication
  unchanged.
- `harden` refuses to run unless `CONFIRM_DEPLOY_SSH_KEY_LOGIN=YES` is set.
- The sshd hardening is installed as
  `/etc/ssh/sshd_config.d/90-patbtawo-hardening.conf`, validated with `sshd -t`,
  checked with `sshd -T`, and then reloaded without stopping existing sessions.
- Root public-key access is preserved with `PermitRootLogin prohibit-password`;
  password and keyboard-interactive authentication are disabled.

## 1. Create or Select a Deploy Key

If a deploy key does not already exist locally, create one:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/patbtawo_deploy -C patbtawo-deploy@pogojar.com
```

Set the local variables used below:

```sh
export ORCHESTRATOR_DEPLOY_HOST=pogojar.com
export ORCHESTRATOR_DEPLOY_USER=deploy
export ORCHESTRATOR_DEPLOY_SSH_KEY_PATH=~/.ssh/patbtawo_deploy
export DEPLOY_PUBLIC_KEY="$(cut -d ' ' -f 1,2 < "${ORCHESTRATOR_DEPLOY_SSH_KEY_PATH}.pub")"
```

## 2. Prepare the Deploy User

Run this while authenticated as root or through an equivalent provider console.
This does not disable global password authentication:

```sh
ssh root@"$ORCHESTRATOR_DEPLOY_HOST" \
  "DEPLOY_USER='$ORCHESTRATOR_DEPLOY_USER' DEPLOY_PUBLIC_KEY='$DEPLOY_PUBLIC_KEY' sh -s -- prepare" \
  < scripts/provision_deploy_user_harden_ssh.sh
```

By default the script adds the deploy user to `www-data` when that group exists.
For additional groups, pass `DEPLOY_EXTRA_GROUPS=www-data,another-group`.

## 3. Confirm Key Login

From a second local terminal, prove that the deploy user can authenticate with
the key and without a password fallback:

```sh
ssh \
  -i "$ORCHESTRATOR_DEPLOY_SSH_KEY_PATH" \
  -o IdentitiesOnly=yes \
  -o PreferredAuthentications=publickey \
  -o PasswordAuthentication=no \
  "$ORCHESTRATOR_DEPLOY_USER@$ORCHESTRATOR_DEPLOY_HOST" \
  'id -un && hostname'
```

Expected first line:

```text
deploy
```

Do not continue until this works.

## 4. Disable Password Authentication

Only after the deploy key login succeeds, run:

```sh
ssh root@"$ORCHESTRATOR_DEPLOY_HOST" \
  "DEPLOY_USER='$ORCHESTRATOR_DEPLOY_USER' CONFIRM_DEPLOY_SSH_KEY_LOGIN=YES sh -s -- harden" \
  < scripts/provision_deploy_user_harden_ssh.sh
```

## 5. Verify Final Access

Run the deploy-user key login command again:

```sh
ssh \
  -i "$ORCHESTRATOR_DEPLOY_SSH_KEY_PATH" \
  -o IdentitiesOnly=yes \
  -o PreferredAuthentications=publickey \
  -o PasswordAuthentication=no \
  "$ORCHESTRATOR_DEPLOY_USER@$ORCHESTRATOR_DEPLOY_HOST" \
  'id && sudo -n true 2>/dev/null || true'
```

Also verify sshd's effective authentication settings from the still-open root
session:

```sh
ssh root@"$ORCHESTRATOR_DEPLOY_HOST" \
  "DEPLOY_USER='$ORCHESTRATOR_DEPLOY_USER' sh -s -- verify" \
  < scripts/provision_deploy_user_harden_ssh.sh
ssh root@"$ORCHESTRATOR_DEPLOY_HOST" \
  "/usr/sbin/sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '"
```

Expected effective values:

```text
passwordauthentication no
kbdinteractiveauthentication no
permitrootlogin prohibit-password
```

Some OpenSSH builds report `permitrootlogin without-password`; that is equivalent
to `prohibit-password`.

## Rollback

If new SSH sessions fail but the original root session is still open, remove the
drop-in and reload sshd:

```sh
rm -f /etc/ssh/sshd_config.d/90-patbtawo-hardening.conf
/usr/sbin/sshd -t && (systemctl reload ssh || systemctl reload sshd || service ssh reload)
```

The `prepare` phase is intentionally non-destructive: it does not remove existing
authorized keys, does not disable root public-key access, and does not change
global password authentication.
