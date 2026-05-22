#!/bin/sh
# Read-only VPS inventory script. Pipe this over SSH; do not run with sudo
# unless you explicitly need process owners for listening sockets.

set -u

section() {
  printf '\n## %s\n' "$1"
}

run() {
  label="$1"
  shift
  printf '\n### %s\n' "$label"
  printf '$'
  printf ' %s' "$@"
  printf '\n'
  "$@" 2>&1 || printf '[command exited with status %s]\n' "$?"
}

run_shell() {
  label="$1"
  command="$2"
  printf '\n### %s\n' "$label"
  printf '$ %s\n' "$command"
  sh -c "$command" 2>&1 || printf '[command exited with status %s]\n' "$?"
}

section "Host"
run "Hostname" hostname
run "Kernel" uname -a
if [ -r /etc/os-release ]; then
  run "OS Release" cat /etc/os-release
else
  printf '\n### OS Release\n/etc/os-release is not readable\n'
fi
if command -v hostnamectl >/dev/null 2>&1; then
  run "Hostnamectl" hostnamectl
fi

section "CPU"
if command -v lscpu >/dev/null 2>&1; then
  run "lscpu" lscpu
else
  run_shell "CPU Count" "getconf _NPROCESSORS_ONLN"
  run_shell "CPU Info" "sed -n '1,80p' /proc/cpuinfo"
fi

section "Memory"
if command -v free >/dev/null 2>&1; then
  run "free -h" free -h
fi
run_shell "Meminfo Summary" "grep -E '^(MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo"

section "Disk"
run_shell "df -hT" "df -hT"
if command -v lsblk >/dev/null 2>&1; then
  run "lsblk" lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
fi

section "Listening Ports"
if command -v ss >/dev/null 2>&1; then
  run "ss -tulpen" ss -tulpen
elif command -v netstat >/dev/null 2>&1; then
  run "netstat -tulpen" netstat -tulpen
else
  printf '\nNeither ss nor netstat is available.\n'
fi

section "Base VPS Packages"
for pkg in curl git build-essential nginx ufw unzip ca-certificates; do
  if command -v dpkg-query >/dev/null 2>&1; then
    run_shell "$pkg package" "dpkg-query -W -f='\${Status} \${Version}\n' $pkg 2>/dev/null || true"
  else
    printf '\n### %s package\ndpkg-query not found\n' "$pkg"
  fi
done
for cmd in curl git gcc g++ make nginx ufw unzip update-ca-certificates; do
  if command -v "$cmd" >/dev/null 2>&1; then
    run "$cmd path" command -v "$cmd"
  else
    printf '\n### %s path\n%s not found in PATH\n' "$cmd" "$cmd"
  fi
done

section "Service Versions"
if command -v nginx >/dev/null 2>&1; then
  run "nginx version" nginx -v
else
  printf '\n### nginx version\nnginx not found in PATH\n'
fi
if command -v node >/dev/null 2>&1; then
  run "node version" node --version
else
  printf '\n### node version\nnode not found in PATH\n'
fi
if command -v npm >/dev/null 2>&1; then
  run "npm version" npm --version
else
  printf '\n### npm version\nnpm not found in PATH\n'
fi
if command -v pnpm >/dev/null 2>&1; then
  run "pnpm version" pnpm --version
else
  printf '\n### pnpm version\npnpm not found in PATH\n'
fi
if command -v pm2 >/dev/null 2>&1; then
  run "pm2 version" pm2 --version
else
  printf '\n### pm2 version\npm2 not found in PATH\n'
fi
if command -v psql >/dev/null 2>&1; then
  run "psql version" psql --version
else
  printf '\n### psql version\npsql not found in PATH\n'
fi
if command -v postgres >/dev/null 2>&1; then
  run "postgres version" postgres --version
else
  printf '\n### postgres version\npostgres not found in PATH\n'
fi
if command -v docker >/dev/null 2>&1; then
  run "docker version" docker --version
else
  printf '\n### docker version\ndocker not found in PATH\n'
fi

section "Systemd Services"
if command -v systemctl >/dev/null 2>&1; then
  run_shell "Running services of interest" "systemctl list-units --type=service --state=running --no-pager | grep -Ei 'nginx|node|pm2|postgres|docker|app|next' || true"
  run_shell "Known service states" "for svc in nginx postgresql postgresql@14-main postgresql@15-main pm2-root pm2-deploy pm2 docker; do systemctl is-active \"\$svc\" 2>/dev/null && printf '%s active\n' \"\$svc\" || true; done"
else
  printf '\nsystemctl is not available.\n'
fi

section "Processes"
run_shell "Processes of interest" "ps -eo pid,user,comm,args --sort=comm | grep -Ei 'nginx|node|pm2|postgres|next|npm|pnpm|docker' | grep -v grep || true"

section "PM2"
if command -v pm2 >/dev/null 2>&1; then
  run "pm2 list" pm2 list
  run "pm2 jlist" pm2 jlist
else
  printf '\npm2 not found in PATH for this user.\n'
fi

section "PostgreSQL"
run_shell "PostgreSQL processes" "ps -eo pid,user,comm,args | grep -Ei 'postgres|postmaster' | grep -v grep || true"
if command -v pg_lsclusters >/dev/null 2>&1; then
  run "pg_lsclusters" pg_lsclusters
fi

section "Docker"
if command -v docker >/dev/null 2>&1; then
  run "docker ps" docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  if docker compose version >/dev/null 2>&1; then
    run "docker compose ls" docker compose ls
  fi
else
  printf '\ndocker not found in PATH for this user.\n'
fi
