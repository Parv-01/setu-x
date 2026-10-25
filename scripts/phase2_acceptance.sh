#!/usr/bin/env bash
# SETU-X Phase 2 acceptance run against a real Docker Compose stack.
# Works on Linux, macOS and Git Bash on Windows. Needs: docker (with compose), curl.
#
# Usage:  bash scripts/phase2_acceptance.sh
# Output: a PASS/FAIL line per check, a summary table, and the full log in
#         docs/evidence/phase2_acceptance.log. Exit code 0 only if every check passed.
# Optional: SETU_BUILD_CMD overrides the build step (default: docker compose --profile test build).
set -uo pipefail

cd "$(dirname "$0")/.."
GW="http://127.0.0.1:${GATEWAY_PORT:-8080}"
LOG_DIR="docs/evidence"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/phase2_acceptance.log"
exec > >(tee "$LOG") 2>&1

RESULTS=()
FAILS=0
ORDER='{"sku":"SKU-001","quantity":2,"customer_tier":"standard"}'

record() { # record <id> <PASS|FAIL> <detail>
  RESULTS+=("$(printf '%-4s %-4s %s' "$1" "$2" "$3")")
  [ "$2" = PASS ] || FAILS=$((FAILS + 1))
  echo ">>> $1 $2 - $3"
}

order() { # prints "<http_code> <seconds>" and writes body to /tmp-like file
  curl -s -o "$BODY" -w '%{http_code} %{time_total}' -X POST "$GW/api/orders" \
    -H 'content-type: application/json' -d "$ORDER"
}

health_of() { docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q "$1")" 2>/dev/null; }

wait_healthy() { # wait_healthy <service> [timeout_s]
  local t=${2:-90}
  for _ in $(seq 1 "$t"); do [ "$(health_of "$1")" = healthy ] && return 0; sleep 1; done
  return 1
}

BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT

echo "SETU-X Phase 2 acceptance"
echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "git_commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown) (uncommitted files outside docs/evidence: $(git status --porcelain -- . ':!docs/evidence' 2>/dev/null | wc -l | tr -d ' '))"
echo "docker: $(docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}' 2>&1)"
echo "compose: $(docker compose version --short 2>&1)"
echo "uname: $(uname -srm)"
echo

echo "=== A1 docker compose config"
if docker compose config --quiet; then record A1 PASS "compose configuration valid"; else record A1 FAIL "compose config invalid"; fi

echo "=== A2 build images"
if ${SETU_BUILD_CMD:-docker compose --profile test build}; then record A2 PASS "images built"; else record A2 FAIL "build failed"; fi
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | grep -E '^setu-x' || true

echo "=== A3 start stack (up -d --wait)"
docker compose down --remove-orphans >/dev/null 2>&1
if docker compose up -d --wait --wait-timeout 120; then record A3 PASS "stack started, compose reports healthy"; else record A3 FAIL "stack did not become healthy"; fi
docker compose ps

echo "=== A4 health status of every service"
bad=""
for s in inventory-a pricing gateway; do st=$(health_of $s); echo "$s: $st"; [ "$st" = healthy ] || bad="$bad $s"; done
if [ -z "$bad" ]; then record A4 PASS "inventory-a, pricing, gateway healthy"; else record A4 FAIL "unhealthy:$bad"; fi

echo "=== A5 health endpoints return documented responses"
live=$(curl -s "$GW/health/live"); ready=$(curl -s "$GW/health/ready"); echo "gateway live:  $live"; echo "gateway ready: $ready"
inv=$(docker compose exec -T gateway python -c "import urllib.request;print(urllib.request.urlopen('http://inventory-a:8000/health/ready',timeout=2).read().decode())")
pri=$(docker compose exec -T gateway python -c "import urllib.request;print(urllib.request.urlopen('http://pricing:8000/health/ready',timeout=2).read().decode())")
echo "inventory-a ready: $inv"; echo "pricing ready:     $pri"
if [[ $live == *'"status":"alive"'* && $ready == *'"status":"ready"'* && $ready == *'"http_client":"ok"'* \
   && $inv == *'"status":"ready"'* && $inv == *'"env":"aws-simulated"'* && $pri == *'"status":"ready"'* && $pri == *'"env":"azure-simulated"'* ]]; then
  record A5 PASS "live/ready bodies match documented shape for all services"
else record A5 FAIL "unexpected health body"; fi

echo "=== A6 request through the gateway produces the expected result"
read -r code t <<<"$(order)"; b=$(cat "$BODY"); echo "HTTP $code in ${t}s: $b"
cp "$BODY" "$LOG_DIR/phase2_sample_order.json"
if [[ $code == 200 && $b == *'"status":"confirmed"'* && $b == *'"available":8'* && $b == *'"total_cents":11000'* && $b == *'"price_source":"pricing-service"'* ]]; then
  record A6 PASS "confirmed; available=8, total_cents=11000 (golden values)"
else record A6 FAIL "unexpected order response (HTTP $code)"; fi

echo "=== A7 automated tests (unit + integration) in the test container"
if docker compose --profile test run --rm tests; then record A7 PASS "unit + integration tests passed"; else record A7 FAIL "unit/integration tests failed"; fi

echo "=== A8 end-to-end tests against the running stack"
if docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e; then record A8 PASS "e2e tests passed"; else record A8 FAIL "e2e tests failed"; fi

echo "=== A9 stopped dependency -> bounded 503, then recovery"
docker compose stop inventory-a
read -r code t <<<"$(order)"; b=$(cat "$BODY"); echo "inventory stopped: HTTP $code in ${t}s: $b"
ok9=0; [[ $code == 503 && $b == *'"code":"dependency_unavailable"'* ]] && awk "BEGIN{exit !($t < 3.5)}" && ok9=1
docker compose start inventory-a && wait_healthy inventory-a 90
read -r code2 t2 <<<"$(order)"; b2=$(cat "$BODY"); echo "after restart: HTTP $code2 in ${t2}s"
if [[ $ok9 == 1 && $code2 == 200 && $b2 == *'"status":"confirmed"'* ]]; then
  record A9 PASS "503 dependency_unavailable in ${t}s (bound 3.5s = outer deadline 3s + slack); recovered to 200"
else record A9 FAIL "stop: HTTP $code in ${t}s; recovery: HTTP $code2"; fi

echo "=== A10 unresponsive (paused) dependency -> 503 cut by timeout, no hang"
docker compose pause inventory-a
read -r code t <<<"$(order)"; b=$(cat "$BODY"); echo "inventory paused: HTTP $code in ${t}s: $b"
docker compose unpause inventory-a
if [[ $code == 503 && $b == *'timeout'* ]] && awk "BEGIN{exit !($t >= 0.8 && $t < 1.6)}"; then
  record A10 PASS "503 (timeout) in ${t}s; expected ~0.8s per-attempt timeout (bound < 1.6s)"
else record A10 FAIL "paused: HTTP $code in ${t}s"; fi

echo "=== A11 pricing stopped -> degraded order with flagged list price"
docker compose stop pricing
read -r code t <<<"$(order)"; b=$(cat "$BODY"); echo "pricing stopped: HTTP $code in ${t}s: $b"
docker compose start pricing && wait_healthy pricing 90
if [[ $code == 200 && $b == *'"status":"degraded"'* && $b == *'"price_source":"fallback-list-price"'* ]]; then
  record A11 PASS "degraded with fallback-list-price in ${t}s"
else record A11 FAIL "HTTP $code"; fi

echo "=== A12 container hardening and limits (read back from docker inspect)"
bad=""
for s in inventory-a pricing gateway; do
  cid=$(docker compose ps -q $s)
  line=$(docker inspect --format 'user={{.Config.User}} ro={{.HostConfig.ReadonlyRootfs}} capdrop={{.HostConfig.CapDrop}} nanocpus={{.HostConfig.NanoCpus}} mem={{.HostConfig.Memory}} secopt={{.HostConfig.SecurityOpt}} privileged={{.HostConfig.Privileged}}' "$cid")
  echo "$s: $line"
  [[ $line == *"user=10001"* && $line == *"ro=true"* && $line == *"capdrop=[ALL]"* && $line == *"nanocpus=500000000"* \
     && $line == *"mem=268435456"* && $line == *"no-new-privileges:true"* && $line == *"privileged=false"* ]] || bad="$bad $s"
done
binding=$(docker port "$(docker compose ps -q gateway)" 8000); echo "gateway port binding: $binding"
[[ $binding == 127.0.0.1:* ]] || bad="$bad gateway-port"
if [ -z "$bad" ]; then record A12 PASS "non-root, read-only rootfs, cap_drop ALL, no-new-privileges, 0.5 CPU / 256MiB, port on 127.0.0.1"
else record A12 FAIL "hardening mismatch:$bad"; fi

echo "=== A13 graceful stop (SIGTERM -> exit 0) and clean restart"
docker compose stop
codes=""; for s in inventory-a pricing gateway; do c=$(docker inspect --format '{{.State.ExitCode}}' "$(docker compose ps -a -q $s)"); codes="$codes $s=$c"; done
echo "exit codes:$codes"
docker compose logs gateway 2>/dev/null | grep -E '"event": "(Shutting down|Finished server process|Waiting for application shutdown.|Application shutdown complete.)' | tail -4
docker compose up -d --wait --wait-timeout 120 >/dev/null 2>&1
read -r code t <<<"$(order)"
if [[ $codes == *"inventory-a=0"* && $codes == *"pricing=0"* && $codes == *"gateway=0"* && $code == 200 ]]; then
  record A13 PASS "all services exited 0 on SIGTERM; restarted healthy; order HTTP 200"
else record A13 FAIL "exit codes:$codes; post-restart HTTP $code"; fi

echo "=== A14 down removes everything; up again from scratch works"
docker compose down
left=$(docker compose ps -a -q | wc -l | tr -d ' ')
docker compose up -d --wait --wait-timeout 120 >/dev/null 2>&1
read -r code t <<<"$(order)"
docker compose logs --no-color gateway 2>/dev/null | grep '"http_request"' | tail -2 > "$LOG_DIR/phase2_gateway_log_sample.jsonl"
docker compose down
if [[ $left == 0 && $code == 200 ]]; then record A14 PASS "down left 0 containers; fresh up served HTTP 200; down again"
else record A14 FAIL "containers left=$left; HTTP $code"; fi

echo
echo "================ SUMMARY ================"
printf '%s\n' "${RESULTS[@]}"
echo "checks: ${#RESULTS[@]}  failed: $FAILS"
[ "$FAILS" -eq 0 ] && echo "PHASE 2 ACCEPTANCE: PASS" || echo "PHASE 2 ACCEPTANCE: FAIL"
exit "$FAILS"
