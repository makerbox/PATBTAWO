#!/bin/sh
# Provision a non-root deploy user and optionally harden OpenSSH.
#
# This script is intentionally two-phase:
#   1. prepare: create the deploy user and install an SSH public key.
#   2. harden: disable password auth only after key login has been confirmed.

set -eu

ACTION="${1:-prepare}"
DEPLOY_USER="${DEPLOY_USER:-${ORCHESTRATOR_DEPLOY_USER:-deploy}}"
DEPLOY_PUBLIC_KEY="${DEPLOY_PUBLIC_KEY:-}"
DEPLOY_EXTRA_GROUPS="${DEPLOY_EXTRA_GROUPS:-www-data}"
DEPLOY_SUDO="${DEPLOY_SUDO:-no}"
CONFIRM_DEPLOY_SSH_KEY_LOGIN="${CONFIRM_DEPLOY_SSH_KEY_LOGIN:-no}"
SSHD_CONFIG="${SSHD_CONFIG:-/etc/ssh/sshd_config}"
SSHD_CONFIG_DROPIN="${SSHD_CONFIG_DROPIN:-/etc/ssh/sshd_config.d/90-patbtawo-hardening.conf}"
SUDOERS_DROPIN="${SUDOERS_DROPIN:-/etc/sudoers.d/90-patbtawo-deploy}"

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

confirm_yes() {
  case "$1" in
    YES|yes|true|TRUE|1) return 0 ;;
    *) return 1 ;;
  esac
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "run this script as root"
  fi
}

validate_deploy_user() {
  if [ -z "$DEPLOY_USER" ] || [ "$DEPLOY_USER" = "root" ]; then
    fail "DEPLOY_USER must be a non-root account name"
  fi
  if ! printf '%s\n' "$DEPLOY_USER" | grep -Eq '^[a-z_][a-z0-9_-]*[$]?$'; then
    fail "DEPLOY_USER has an unsafe account name: $DEPLOY_USER"
  fi
}

validate_public_key() {
  if [ -z "$DEPLOY_PUBLIC_KEY" ]; then
    fail "DEPLOY_PUBLIC_KEY is required for prepare"
  fi
  if printf '%s\n' "$DEPLOY_PUBLIC_KEY" | grep -q '[[:cntrl:]]'; then
    fail "DEPLOY_PUBLIC_KEY must be a single-line OpenSSH public key"
  fi

  key_type=$(printf '%s\n' "$DEPLOY_PUBLIC_KEY" | awk '{print $1}')
  case "$key_type" in
    ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)
      ;;
    *)
      fail "DEPLOY_PUBLIC_KEY does not look like a supported OpenSSH public key"
      ;;
  esac
}

install_deploy_user() {
  if id "$DEPLOY_USER" >/dev/null 2>&1; then
    log "deploy user already exists: $DEPLOY_USER"
  else
    useradd --create-home --shell /bin/bash --user-group "$DEPLOY_USER"
    log "created deploy user: $DEPLOY_USER"
  fi

  passwd -l "$DEPLOY_USER" >/dev/null 2>&1 || warn "could not lock password for $DEPLOY_USER"

  home_dir=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
  if [ -z "$home_dir" ] || [ ! -d "$home_dir" ]; then
    fail "home directory for $DEPLOY_USER was not created"
  fi

  ssh_dir="$home_dir/.ssh"
  auth_keys="$ssh_dir/authorized_keys"
  install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$ssh_dir"
  touch "$auth_keys"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$auth_keys"
  chmod 0600 "$auth_keys"

  if grep -qxF "$DEPLOY_PUBLIC_KEY" "$auth_keys"; then
    log "deploy public key already present"
  else
    printf '%s\n' "$DEPLOY_PUBLIC_KEY" >> "$auth_keys"
    log "installed deploy public key"
  fi
  chown "$DEPLOY_USER:$DEPLOY_USER" "$auth_keys"
  chmod 0600 "$auth_keys"

  for group in $(printf '%s\n' "$DEPLOY_EXTRA_GROUPS" | tr ',' ' '); do
    [ -n "$group" ] || continue
    if getent group "$group" >/dev/null 2>&1; then
      usermod -aG "$group" "$DEPLOY_USER"
      log "ensured $DEPLOY_USER is in group: $group"
    else
      warn "group does not exist, skipping: $group"
    fi
  done
}

configure_sudo_if_requested() {
  if ! confirm_yes "$DEPLOY_SUDO"; then
    return 0
  fi
  if ! command -v visudo >/dev/null 2>&1; then
    fail "visudo is required before writing sudoers"
  fi

  tmp_file=$(mktemp)
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$DEPLOY_USER" > "$tmp_file"
  chmod 0440 "$tmp_file"
  visudo -cf "$tmp_file" >/dev/null
  install -m 0440 "$tmp_file" "$SUDOERS_DROPIN"
  rm -f "$tmp_file"
  log "installed sudoers drop-in: $SUDOERS_DROPIN"
}

sshd_binary() {
  if command -v sshd >/dev/null 2>&1; then
    command -v sshd
    return 0
  fi
  if [ -x /usr/sbin/sshd ]; then
    printf '%s\n' /usr/sbin/sshd
    return 0
  fi
  fail "sshd binary was not found"
}

validate_sshd_config() {
  sshd_bin=$(sshd_binary)
  "$sshd_bin" -t -f "$SSHD_CONFIG"
}

effective_sshd_value() {
  key="$1"
  sshd_bin=$(sshd_binary)
  "$sshd_bin" -T -f "$SSHD_CONFIG" 2>/dev/null | awk -v key="$key" '$1 == key {print $2; exit}'
}

require_sshd_dropin_include() {
  if [ ! -f "$SSHD_CONFIG" ]; then
    fail "sshd config was not found at $SSHD_CONFIG"
  fi
  if [ ! -d "$(dirname "$SSHD_CONFIG_DROPIN")" ]; then
    install -d -m 0755 "$(dirname "$SSHD_CONFIG_DROPIN")"
  fi
  if ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "$SSHD_CONFIG"; then
    fail "$SSHD_CONFIG does not include /etc/ssh/sshd_config.d/*.conf; refusing to edit the main sshd_config automatically"
  fi
}

write_hardening_dropin() {
  tmp_file=$(mktemp)
  cat > "$tmp_file" <<'EOF'
# Managed by PATBTAWO.
# Keep root public-key access available while disabling password-based login.
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
EOF
  chmod 0644 "$tmp_file"

  if [ -e "$SSHD_CONFIG_DROPIN" ]; then
    backup_path="$SSHD_CONFIG_DROPIN.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$SSHD_CONFIG_DROPIN" "$backup_path"
    log "backed up existing sshd drop-in: $backup_path"
  fi

  install -m 0644 "$tmp_file" "$SSHD_CONFIG_DROPIN"
  rm -f "$tmp_file"
  log "installed sshd hardening drop-in: $SSHD_CONFIG_DROPIN"
}

verify_hardening_effective() {
  password_auth=$(effective_sshd_value passwordauthentication)
  kbd_auth=$(effective_sshd_value kbdinteractiveauthentication)
  root_login=$(effective_sshd_value permitrootlogin)

  [ "$password_auth" = "no" ] || fail "effective sshd PasswordAuthentication is '$password_auth', expected 'no'"
  [ "$kbd_auth" = "no" ] || fail "effective sshd KbdInteractiveAuthentication is '$kbd_auth', expected 'no'"
  case "$root_login" in
    prohibit-password|without-password) ;;
    *) fail "effective sshd PermitRootLogin is '$root_login', expected 'prohibit-password'" ;;
  esac
}

reload_sshd() {
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl reload ssh >/dev/null 2>&1; then
      log "reloaded ssh service"
      return 0
    fi
    if systemctl reload sshd >/dev/null 2>&1; then
      log "reloaded sshd service"
      return 0
    fi
  fi
  if command -v service >/dev/null 2>&1; then
    if service ssh reload >/dev/null 2>&1; then
      log "reloaded ssh service"
      return 0
    fi
    if service sshd reload >/dev/null 2>&1; then
      log "reloaded sshd service"
      return 0
    fi
  fi
  if command -v pkill >/dev/null 2>&1 && pkill -HUP sshd >/dev/null 2>&1; then
    log "sent HUP to sshd"
    return 0
  fi
  fail "could not reload sshd; config is valid but may not be active yet"
}

prepare() {
  require_root
  validate_deploy_user
  validate_public_key
  install_deploy_user
  configure_sudo_if_requested
  validate_sshd_config
  log "prepare complete"
  log "before hardening, confirm key login from another terminal with password auth disabled client-side"
}

harden() {
  require_root
  validate_deploy_user
  if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    fail "deploy user does not exist: $DEPLOY_USER"
  fi
  if ! confirm_yes "$CONFIRM_DEPLOY_SSH_KEY_LOGIN"; then
    fail "refusing to disable password auth until CONFIRM_DEPLOY_SSH_KEY_LOGIN=YES is set"
  fi

  require_sshd_dropin_include
  validate_sshd_config
  write_hardening_dropin
  validate_sshd_config
  verify_hardening_effective
  reload_sshd
  log "hardening complete"
}

verify() {
  require_root
  validate_deploy_user
  id "$DEPLOY_USER" >/dev/null 2>&1 || fail "deploy user does not exist: $DEPLOY_USER"
  home_dir=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
  [ -n "$home_dir" ] && [ -d "$home_dir/.ssh" ] || fail "deploy user .ssh directory is missing"
  [ -f "$home_dir/.ssh/authorized_keys" ] || fail "deploy user authorized_keys is missing"
  validate_sshd_config
  log "deploy user exists and sshd config syntax is valid"
  if [ -f "$SSHD_CONFIG_DROPIN" ]; then
    log "hardening drop-in exists: $SSHD_CONFIG_DROPIN"
    log "effective PasswordAuthentication: $(effective_sshd_value passwordauthentication)"
    log "effective KbdInteractiveAuthentication: $(effective_sshd_value kbdinteractiveauthentication)"
    log "effective PermitRootLogin: $(effective_sshd_value permitrootlogin)"
  fi
}

case "$ACTION" in
  prepare) prepare ;;
  harden) harden ;;
  verify) verify ;;
  *)
    fail "usage: $0 [prepare|harden|verify]"
    ;;
esac
