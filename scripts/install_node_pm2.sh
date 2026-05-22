#!/bin/sh
# Install and verify the Node.js LTS runtime plus PM2 on an Ubuntu/Debian VPS.

set -eu

ACTION="${1:-install}"
NODE_LTS_MAJOR="${NODE_LTS_MAJOR:-24}"
NODE_VERSION_PREFIX="${NODE_VERSION_PREFIX:-v${NODE_LTS_MAJOR}.}"
NODE_DIST_BASE="${NODE_DIST_BASE:-https://nodejs.org/dist/latest-v${NODE_LTS_MAJOR}.x}"
NODE_INSTALL_DIR="${NODE_INSTALL_DIR:-/usr/local/lib/nodejs}"
NODE_SYMLINK_DIR="${NODE_SYMLINK_DIR:-/usr/local/bin}"
NODE_DOWNLOAD_PACKAGES="${NODE_DOWNLOAD_PACKAGES:-ca-certificates curl xz-utils}"
APT_GET="${APT_GET:-apt-get}"
CONFIGURE_PM2_STARTUP="${CONFIGURE_PM2_STARTUP:-yes}"
INSTALL_PM2="${INSTALL_PM2:-yes}"
PM2_NPM_PACKAGE="${PM2_NPM_PACKAGE:-pm2@latest}"
DEFAULT_PM2_SERVICE_USER="${ORCHESTRATOR_DEPLOY_USER:-deploy}"
PM2_SERVICE_USER="${PM2_SERVICE_USER:-$DEFAULT_PM2_SERVICE_USER}"
PM2_SERVICE_HOME="${PM2_SERVICE_HOME:-}"
PATH="$NODE_SYMLINK_DIR:$PATH"
export PATH

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

run() {
  printf '+'
  for arg in "$@"; do
    printf ' %s' "$arg"
  done
  printf '\n'
  "$@"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "run this script as root, or through sudo: sudo sh scripts/install_node_pm2.sh"
  fi
}

require_apt() {
  command -v "$APT_GET" >/dev/null 2>&1 || fail "$APT_GET was not found; this script supports apt-based Ubuntu/Debian hosts"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

validate_pm2_service_user() {
  if [ -z "$PM2_SERVICE_USER" ]; then
    fail "PM2_SERVICE_USER must not be empty"
  fi
  if ! printf '%s\n' "$PM2_SERVICE_USER" | grep -Eq '^[a-z_][a-z0-9_-]*[$]?$'; then
    fail "PM2_SERVICE_USER has an unsafe account name: $PM2_SERVICE_USER"
  fi
  id "$PM2_SERVICE_USER" >/dev/null 2>&1 || fail "PM2_SERVICE_USER does not exist: $PM2_SERVICE_USER"

  if [ -z "$PM2_SERVICE_HOME" ]; then
    PM2_SERVICE_HOME=$(getent passwd "$PM2_SERVICE_USER" | cut -d: -f6)
  fi
  [ -n "$PM2_SERVICE_HOME" ] && [ -d "$PM2_SERVICE_HOME" ] || fail "home directory for $PM2_SERVICE_USER was not found"
}

detect_node_arch() {
  machine=$(uname -m)
  case "$machine" in
    x86_64|amd64) printf '%s\n' x64 ;;
    aarch64|arm64) printf '%s\n' arm64 ;;
    armv7l) printf '%s\n' armv7l ;;
    *)
      fail "unsupported Node.js Linux architecture: $machine"
      ;;
  esac
}

download_latest_node_tarball() {
  node_arch="$1"
  tmp_dir="$2"
  sums_path="$tmp_dir/SHASUMS256.txt"

  run curl -fsSL "$NODE_DIST_BASE/SHASUMS256.txt" -o "$sums_path"
  tar_name=$(awk -v arch="$node_arch" '$2 ~ "^node-v[0-9.]+-linux-" arch "\\.tar\\.xz$" { print $2; exit }' "$sums_path")
  [ -n "$tar_name" ] || fail "could not find a linux-$node_arch tarball in $NODE_DIST_BASE/SHASUMS256.txt"

  node_version=$(printf '%s\n' "$tar_name" | sed -E 's/^node-(v[0-9][^-]+)-linux-.*/\1/')
  case "$node_version" in
    "$NODE_VERSION_PREFIX"*) ;;
    *) fail "downloaded Node.js version $node_version does not match expected LTS prefix $NODE_VERSION_PREFIX" ;;
  esac

  run curl -fsSL "$NODE_DIST_BASE/$tar_name" -o "$tmp_dir/$tar_name"
  expected_sha=$(awk -v tar="$tar_name" '$2 == tar { print $1; exit }' "$sums_path")
  [ -n "$expected_sha" ] || fail "could not find checksum for $tar_name"
  (cd "$tmp_dir" && printf '%s  %s\n' "$expected_sha" "$tar_name" | sha256sum -c -)

  NODE_TARBALL_NAME="$tar_name"
}

install_node_tarball() {
  tar_name="$1"
  tmp_dir="$2"
  extracted_name=${tar_name%.tar.xz}
  extracted_path="$NODE_INSTALL_DIR/$extracted_name"

  install -d -m 0755 "$NODE_INSTALL_DIR"
  install -d -m 0755 "$NODE_SYMLINK_DIR"
  if [ -d "$extracted_path" ]; then
    log "Node.js archive already extracted: $extracted_path"
  else
    run tar -xJf "$tmp_dir/$tar_name" -C "$NODE_INSTALL_DIR"
  fi

  run ln -sfn "$extracted_path" "$NODE_INSTALL_DIR/current"
  for bin in node npm npx corepack; do
    if [ -x "$NODE_INSTALL_DIR/current/bin/$bin" ]; then
      run ln -sfn "$NODE_INSTALL_DIR/current/bin/$bin" "$NODE_SYMLINK_DIR/$bin"
    fi
  done
}

npm_install_pm2() {
  if ! confirm_yes "$INSTALL_PM2"; then
    log "INSTALL_PM2=$INSTALL_PM2; leaving PM2 installation unchanged"
    return 0
  fi

  run env "PATH=$NODE_SYMLINK_DIR:$PATH" "NPM_CONFIG_PREFIX=$NODE_INSTALL_DIR/current" npm install -g "$PM2_NPM_PACKAGE"
  if [ -x "$NODE_INSTALL_DIR/current/bin/pm2" ]; then
    run ln -sfn "$NODE_INSTALL_DIR/current/bin/pm2" "$NODE_SYMLINK_DIR/pm2"
  fi
}

run_pm2_as_service_user() {
  pm2_home="$PM2_SERVICE_HOME/.pm2"
  user_group=$(id -gn "$PM2_SERVICE_USER")
  install -d -m 0755 -o "$PM2_SERVICE_USER" -g "$user_group" "$pm2_home"

  if [ "$(id -u "$PM2_SERVICE_USER")" -eq 0 ]; then
    run env "PATH=$NODE_SYMLINK_DIR:$PATH" "PM2_HOME=$pm2_home" "$@"
    return 0
  fi

  require_command runuser
  run runuser -u "$PM2_SERVICE_USER" -- env "PATH=$NODE_SYMLINK_DIR:$PATH" "PM2_HOME=$pm2_home" "$@"
}

configure_pm2_startup() {
  if ! confirm_yes "$CONFIGURE_PM2_STARTUP"; then
    log "CONFIGURE_PM2_STARTUP=$CONFIGURE_PM2_STARTUP; leaving PM2 startup unchanged"
    return 0
  fi

  validate_pm2_service_user
  require_command systemctl
  require_command pm2

  run env "PATH=$NODE_SYMLINK_DIR:$PATH" pm2 startup systemd -u "$PM2_SERVICE_USER" --hp "$PM2_SERVICE_HOME"
  run_pm2_as_service_user pm2 save --force
}

report_command_first_line() {
  label="$1"
  command="$2"
  output=$(sh -c "$command" 2>&1 || printf '[command exited with status %s]' "$?")
  first_line=$(printf '%s\n' "$output" | sed -n '1p')
  if [ -n "$first_line" ]; then
    printf '%-24s %s\n' "$label" "$first_line"
  else
    printf '%-24s no output\n' "$label"
  fi
}

verify_node_pm2() {
  validate_pm2_service_user
  missing=0

  printf '\n## Required Command Paths\n'
  for cmd in node npm npx pm2; do
    if path=$(command -v "$cmd" 2>/dev/null); then
      printf '%-24s %s\n' "$cmd" "$path"
    else
      printf '%-24s missing\n' "$cmd"
      missing=1
    fi
  done

  printf '\n## Command Versions\n'
  report_command_first_line "node" "node --version"
  report_command_first_line "npm" "npm --version"
  report_command_first_line "pm2" "pm2 --version"

  node_version=$(node --version 2>/dev/null || true)
  case "$node_version" in
    "$NODE_VERSION_PREFIX"*) ;;
    *)
      printf '%-24s expected %s*, got %s\n' "node LTS major" "$NODE_VERSION_PREFIX" "${node_version:-missing}"
      missing=1
      ;;
  esac

  if env "NPM_CONFIG_PREFIX=$NODE_INSTALL_DIR/current" npm list -g --depth=0 pm2 >/dev/null 2>&1; then
    printf '%-24s installed globally\n' "pm2 npm package"
  else
    printf '%-24s missing from npm global list\n' "pm2 npm package"
    missing=1
  fi

  printf '\n## PM2 Startup\n'
  service_name="pm2-$PM2_SERVICE_USER"
  pm2_dump="$PM2_SERVICE_HOME/.pm2/dump.pm2"
  if command -v systemctl >/dev/null 2>&1; then
    service_enabled=$(systemctl is-enabled "$service_name" 2>/dev/null || true)
    service_active=$(systemctl is-active "$service_name" 2>/dev/null || true)
    printf '%-24s %s\n' "$service_name enabled" "${service_enabled:-unknown}"
    printf '%-24s %s\n' "$service_name active" "${service_active:-unknown}"
    if [ "$service_enabled" != "enabled" ]; then
      missing=1
    fi
  else
    printf 'systemctl not available; cannot verify PM2 systemd startup\n'
    missing=1
  fi

  if [ -f "$pm2_dump" ]; then
    printf '%-24s %s\n' "pm2 saved process list" "$pm2_dump"
  else
    printf '%-24s missing at %s\n' "pm2 saved process list" "$pm2_dump"
    missing=1
  fi

  run_pm2_as_service_user pm2 list

  if [ "$missing" -ne 0 ]; then
    fail "one or more required Node.js, npm, PM2, or startup checks failed"
  fi
  log "Node.js LTS and PM2 verification complete"
}

install_runtime() {
  require_root
  require_apt
  validate_pm2_service_user
  export DEBIAN_FRONTEND=noninteractive

  run "$APT_GET" update
  # shellcheck disable=SC2086
  run "$APT_GET" install -y $NODE_DOWNLOAD_PACKAGES
  require_command awk
  require_command curl
  require_command sha256sum
  require_command tar

  node_arch=$(detect_node_arch)
  tmp_dir=$(mktemp -d)
  trap 'rm -rf "$tmp_dir"' EXIT INT TERM
  download_latest_node_tarball "$node_arch" "$tmp_dir"
  install_node_tarball "$NODE_TARBALL_NAME" "$tmp_dir"
  npm_install_pm2
  configure_pm2_startup
  verify_node_pm2
}

case "$ACTION" in
  install) install_runtime ;;
  verify|status) verify_node_pm2 ;;
  *)
    fail "usage: $0 [install|verify]"
    ;;
esac
