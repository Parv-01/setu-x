#!/usr/bin/env bash
# SETU-X environment check (Linux/macOS, or Git Bash/WSL on Windows).
# Reports what is installed and usable. It never reports a missing tool as OK.
# Exit code: 0 if the required tools (docker engine reachable, docker compose) work, 1 otherwise.
set -u

required_fail=0

check() {  # check <label> <required:0|1> <command...>
  local label="$1" required="$2"; shift 2
  local out
  if out=$("$@" 2>&1); then
    printf "  [ OK ] %-22s %s\n" "$label" "$(printf '%s' "$out" | head -n1)"
  else
    if [ "$required" = 1 ]; then
      printf "  [FAIL] %-22s %s\n" "$label" "$(printf '%s' "$out" | grep -v '^[[:space:]]*$' | tail -n1 | cut -c1-120)"
      required_fail=1
    else
      printf "  [ -- ] %-22s not available (optional)\n" "$label"
    fi
  fi
}

echo "SETU-X environment check  ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "Required:"
check "docker client"        1 docker --version
check "docker engine"        1 docker version --format 'server {{.Server.Version}}'
check "docker compose"       1 docker compose version
echo "Optional (only for running tests or tools outside containers):"
check "git"                  0 git --version
check "python3"              0 python3 --version
check "make"                 0 make --version

if [ "$required_fail" -ne 0 ]; then
  echo "Result: required tools missing or Docker engine not running. Start Docker Desktop / dockerd and retry."
  exit 1
fi
echo "Result: required tools available."
