#!/bin/sh
# Install and verify the base Ubuntu/Debian VPS packages needed for app hosting.

set -eu

ACTION="${1:-install}"
BASE_VPS_PACKAGES="${BASE_VPS_PACKAGES:-curl git build-essential nginx ufw unzip ca-certificates}"
APT_GET="${APT_GET:-apt-get}"
START_NGINX="${START_NGINX:-yes}"

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
    fail "run this script as root, or through sudo: sudo sh scripts/install_base_vps_packages.sh"
  fi
}

require_apt() {
  command -v "$APT_GET" >/dev/null 2>&1 || fail "$APT_GET was not found; this script supports apt-based Ubuntu/Debian hosts"
  command -v dpkg-query >/dev/null 2>&1 || fail "dpkg-query was not found"
}

ensure_nginx_started() {
  if ! command -v nginx >/dev/null 2>&1; then
    warn "nginx command is not available after package installation"
    return 0
  fi

  if command -v systemctl >/dev/null 2>&1; then
    if systemctl enable --now nginx >/dev/null 2>&1; then
      log "nginx service is enabled and started"
    else
      warn "could not enable/start nginx with systemctl; verify service status below"
    fi
    return 0
  fi

  if command -v service >/dev/null 2>&1; then
    if service nginx start >/dev/null 2>&1; then
      log "nginx service start requested"
    else
      warn "could not start nginx with service; verify service status below"
    fi
  fi
}

package_version() {
  pkg="$1"
  dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || true
}

package_status() {
  pkg="$1"
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true
}

report_command_first_line() {
  label="$1"
  command="$2"
  output=$(sh -c "$command" 2>&1 || printf '[command exited with status %s]' "$?")
  first_line=$(printf '%s\n' "$output" | sed -n '1p')
  if [ -n "$first_line" ]; then
    printf '%-22s %s\n' "$label" "$first_line"
  else
    printf '%-22s no output\n' "$label"
  fi
}

verify_packages() {
  missing=0

  printf '\n## Required Package Status\n'
  for pkg in $BASE_VPS_PACKAGES; do
    status=$(package_status "$pkg")
    version=$(package_version "$pkg")
    if [ "$status" = "install ok installed" ]; then
      printf '%-22s installed %s\n' "$pkg" "$version"
    else
      printf '%-22s missing %s\n' "$pkg" "$status"
      missing=1
    fi
  done

  printf '\n## Required Command Paths\n'
  for cmd in curl git gcc g++ make nginx ufw unzip update-ca-certificates; do
    if path=$(command -v "$cmd" 2>/dev/null); then
      printf '%-22s %s\n' "$cmd" "$path"
    else
      printf '%-22s missing\n' "$cmd"
      missing=1
    fi
  done

  printf '\n## Command Versions\n'
  report_command_first_line "curl" "curl --version"
  report_command_first_line "git" "git --version"
  report_command_first_line "gcc" "gcc --version"
  report_command_first_line "g++" "g++ --version"
  report_command_first_line "make" "make --version"
  report_command_first_line "nginx" "nginx -v"
  report_command_first_line "ufw" "ufw --version"
  report_command_first_line "unzip" "unzip -v"

  if [ "$missing" -ne 0 ]; then
    fail "one or more required base VPS packages or commands are missing"
  fi
}

report_service_status() {
  printf '\n## Service Status\n'

  if command -v systemctl >/dev/null 2>&1; then
    nginx_active=$(systemctl is-active nginx 2>/dev/null || true)
    nginx_enabled=$(systemctl is-enabled nginx 2>/dev/null || true)
    ufw_active=$(systemctl is-active ufw 2>/dev/null || true)
    ufw_enabled=$(systemctl is-enabled ufw 2>/dev/null || true)
    printf '%-22s %s\n' "nginx active" "${nginx_active:-unknown}"
    printf '%-22s %s\n' "nginx enabled" "${nginx_enabled:-unknown}"
    printf '%-22s %s\n' "ufw active" "${ufw_active:-unknown}"
    printf '%-22s %s\n' "ufw enabled" "${ufw_enabled:-unknown}"
  else
    printf 'systemctl not available; skipping systemd service state checks\n'
  fi

  if command -v nginx >/dev/null 2>&1; then
    printf '\n### nginx config test\n'
    nginx -t 2>&1 || warn "nginx config test failed"
  fi

  if command -v ufw >/dev/null 2>&1; then
    printf '\n### ufw status\n'
    ufw status verbose 2>&1 || warn "ufw status failed"
  fi
}

verify() {
  require_apt
  verify_packages
  report_service_status
  log "base VPS package verification complete"
}

install_packages() {
  require_root
  require_apt
  export DEBIAN_FRONTEND=noninteractive

  run "$APT_GET" update
  # shellcheck disable=SC2086
  run "$APT_GET" install -y $BASE_VPS_PACKAGES

  if confirm_yes "$START_NGINX"; then
    ensure_nginx_started
  else
    log "START_NGINX=$START_NGINX; leaving nginx service state unchanged"
  fi

  log "ufw was installed but not enabled; configure allow rules before enabling it"
  verify
}

case "$ACTION" in
  install) install_packages ;;
  verify|status) verify ;;
  *)
    fail "usage: $0 [install|verify]"
    ;;
esac
