# Testing SETU-X on Windows with VS Code

For: Windows 10/11, PowerShell 7, Docker Desktop, VS Code.
Each step says from which phase it works. A step marked **(Phase N)** depends on code that does not exist yet. Running it earlier will fail, and that is expected.

---

## 0. One-time setup

1. **Docker Desktop must be running.** Open Docker Desktop and wait for **"Engine running"**. Then check, in PowerShell:
   ```powershell
   docker version          # must show BOTH "Client:" and "Server:" sections
   docker compose version  # v5.1.3 or later
   docker run --rm hello-world
   ```
   If you see `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`, the engine isn't running. See Troubleshooting (§7).
2. **VS Code extensions** (Extensions panel, `Ctrl+Shift+X`):
   - *Python* (Microsoft): runs and debugs tests from the Testing panel.
   - *Container Tools* (Microsoft, formerly the Docker extension): lists containers, shows logs, opens a shell.
   - *YAML* (Red Hat), optional: validates the config files.
3. **Open the project folder:** `File → Open Folder… → E:\Projects\Cloud_Mini_Project\setu-x`.
4. **Use PowerShell 7 as the VS Code terminal:** `` Ctrl+` `` opens it. All commands below are run from the `setu-x` folder.

You do **not** need Python installed on Windows to run the stack or its tests. Everything can run inside containers. A local Python environment is only needed for VS Code's Testing panel (§4).

## 1. Check the environment (works now)

```powershell
docker version; docker compose version
```
In Git Bash or WSL you can also run `./scripts/check_env.sh` (exit code 0 = required tools OK).

## 1b. Phase 2 acceptance in one command (available now)

With Docker Desktop running, from the `setu-x` folder in PowerShell 7:
```powershell
pwsh -ExecutionPolicy Bypass -File scripts\phase2_acceptance.ps1
```
It builds the images, starts the stack and runs 14 checks:
- **A1–A5:** config valid, build, start, every service healthy, health endpoints.
- **A6:** an order returns the expected result.
- **A7–A8:** unit/integration tests and end-to-end tests.
- **A9:** a stopped inventory service gives a fast 503, then the stack recovers.
- **A10:** a paused (unresponsive) inventory service gives a 503 after about 0.8 s.
- **A11:** a stopped pricing service gives a degraded order.
- **A12:** container hardening, read back from `docker inspect`.
- **A13–A14:** graceful shutdown with exit 0, and a clean restart.

The run ends with `PHASE 2 ACCEPTANCE: PASS` or `FAIL` and writes the full log to `docs\evidence\phase2_acceptance_windows.log`. **Please send me that log file.** It is the evidence that the project runs on your machine. (The same checks exist for Git Bash, Linux and macOS as `scripts/phase2_acceptance.sh`.)

The first run downloads the Python base image and packages, so it takes a few minutes. Later runs use the cache.

## 1c. Phase 3 acceptance (available now)

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\phase3_acceptance.ps1
```
15 checks:
- **B1–B4:** config valid; images build; `configs/` validates, and a deliberately broken config is rejected with the field named; all five services healthy (inventory-a, inventory-b, pricing, controller, gateway).
- **B5–B6:** unit, contract, integration and end-to-end tests.
- **B7–B8:** simulated environments are labelled everywhere; the AWS/Azure/GCP adapters honestly report `not_configured`.
- **B9:** the placement API rejects invalid changes.
- **B10–B12:** round-robin, weighted 3:1, and disabled-primary routing.
- **B13:** the gateway keeps serving while the controller is stopped.
- **B14:** container hardening.
- **B15:** clean shutdown.

The log goes to `docs\evidence\phase3_acceptance_windows.log`. Please send it to me, as you did for Phase 2.

Phase 3 also adds the **controller** on http://127.0.0.1:8090. Try http://127.0.0.1:8090/docs, `GET /api/environments`, `GET /api/adapters`, `GET /api/services` and `GET /api/placement`. On the gateway, `GET /api/routing/decisions` shows recent routing choices with reasons.

To validate your own edits to `configs\*.yaml` before starting anything:
```powershell
docker compose --profile test run --rm --no-deps tests python -m setu_x.config_check configs
```

## 1d. Phase 4 acceptance (available now)

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\phase4_acceptance.ps1
```
15 checks (C1–C15):
- Build, health and the full test suite.
- Fault limits enforced.
- An error fault on inventory-a triggers fallback to inventory-b.
- The same fault with fallback off gives a fast 503.
- Bounded retries under a 50% transient fault: inventory-a receives exactly as many requests as the gateway attempted.
- A hang is cut at the 0.8 s timeout.
- The circuit breaker opens after 3 failures, stops calls to inventory-a, and recovers.
- A really stopped container is handled by fallback.
- **Simulated mode:** the gateway is restarted with `SETU_MODE=simulated` and the injected 40/55 ms link delay is checked.
- Events are recorded, and the stack shuts down cleanly.

Log: `docs\evidence\phase4_acceptance_windows.log`. Please send it to me.

The script takes about 2–3 minutes. It sets `SETU_MODE` for its own session only and restores local mode at the end.

**Note:** `scripts\phase2_acceptance.ps1` and `.sh` are a Phase 2 snapshot. Since Phase 4, fallback is on by default, so their checks A9/A10 (expecting a 503 when inventory-a is stopped or paused) now get a 200 from inventory-b. That is the intended new behaviour. Phase 4 checks C8 and C10 cover the 503 path with fallback turned off.

**Trying faults by hand** (stack running):
```powershell
# make inventory-a fail every request for 30 s, then watch orders fall back to inventory-b
Invoke-RestMethod -Method Post http://127.0.0.1:8090/api/faults -ContentType 'application/json' `
  -Body '{"target":"inventory-a","mode":"error","duration_s":30}'
Invoke-RestMethod -Method Post http://127.0.0.1:8080/api/orders -ContentType 'application/json' `
  -Body '{"sku":"SKU-002","quantity":1}' | Select-Object -ExpandProperty inventory
Invoke-RestMethod http://127.0.0.1:8080/api/resilience/events     # retries, fallbacks, breaker transitions
Invoke-RestMethod -Method Delete http://127.0.0.1:8090/api/faults   # clear all faults

# stop a container for 20 s (real process failure)
pwsh -File scripts\fault_container.ps1 -Service inventory-a -Seconds 20

# simulated network delay: restart only the gateway in simulated mode
$env:SETU_MODE='simulated'; docker compose up -d --no-deps --wait gateway
```

## 2. Start, inspect and stop the stack (Phase 2, available now)

```powershell
docker compose config            # validates compose.yaml; prints the resolved file
docker compose up --build -d     # builds images and starts all services in the background
docker compose ps                # every service should show "running (healthy)"
docker compose logs -f gateway   # follow one service's logs (Ctrl+C to stop following)
docker compose down              # stop and remove containers (images stay cached)
```

In VS Code you can see the same thing in the **Container Tools** side bar. Right-click a container for **View Logs** or **Attach Shell**.

## 3. Manual smoke test in the browser (API available now; dashboard in Phase 6)

| What | URL | Expected |
|---|---|---|
| Gateway health | http://127.0.0.1:8080/health/ready | JSON with `"status": "ready"` |
| Interactive API docs | http://127.0.0.1:8080/docs | Swagger page. Expand `POST /api/orders` → *Try it out* → *Execute*. You should get `"status": "confirmed"` |
| Dashboard | http://127.0.0.1:8090 | Service health, placement, experiment controls (Phase 6) |

The same order request from PowerShell:
```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/orders `
  -ContentType 'application/json' -Body '{"sku":"SKU-001","quantity":2,"customer_tier":"standard"}'
```

## 4. Automated tests

### 4a. Inside Docker: recommended, no local Python needed (Phase 2)
```powershell
docker compose --profile test run --rm tests                                     # unit + integration (contract tests arrive in Phase 3)
docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e   # end-to-end against the running stack
docker compose --profile test run --rm tests ruff check --no-cache src tests     # lint
```
The summary line at the end (`N passed`, or failures with details) is the result.

### 4b. In VS Code's Testing panel (Phase 2)

Create a Python 3.11 environment once, with either the Python launcher or conda:
```powershell
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
# or:  conda create -n setu-x python=3.11 -y; conda activate setu-x
pip install -e ".[dev]"
pytest -m "not e2e"
```
Then in VS Code:
1. `Ctrl+Shift+P` → **Python: Select Interpreter** → choose `.venv` (or `setu-x`).
2. Open the **Testing** panel (flask icon). The repo's `.vscode/settings.json` points it at pytest and `tests/` (excluding `e2e`). `.vscode/extensions.json` makes VS Code suggest the three extensions above.
3. Click ▶ to run all tests, or ▶ next to a single test. Use the bug icon to debug with breakpoints.

End-to-end tests (`-m e2e`) need the stack running (`docker compose up -d`) first.

## 5. Run one experiment (Phase 5)

```powershell
docker compose exec controller python -m setu_x.experiments.runner --scenario configs/scenarios/A_baseline.yaml
dir results\    # a new folder named by experiment ID, with manifest.json, observations.csv, summary.json
```
Or press **Start** on the dashboard (Phase 6).

## 6. "Is everything working and complete?" One command (Phase 7)

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\verify.ps1
```
This runs the full gate:
1. Compose config check.
2. Build and start.
3. Wait for healthy services.
4. Unit, contract and integration tests.
5. End-to-end tests.
6. A short baseline experiment, plus checks that its files reconcile.
7. Stop and restart, then down.

It prints a PASS/FAIL table and exits non-zero on any failure. Its output on your machine is the evidence that the project runs on your computer. Please send it to me. `docs/validation_report.md` lists which deliverable each check covers.

**Completeness checklist.** The project brief's §15 checklist is mirrored in `docs/validation_report.md` (Phase 7). Each item links to the file or command that proves it.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine` | Docker Desktop isn't running. Start it and wait for "Engine running". If it won't start: run `wsl --status`, `wsl --update` and `wsl -l -v` (a `docker-desktop` distro should be listed); check Task Manager → Performance → CPU → *Virtualization: Enabled*; in Docker Desktop → Settings → General, tick *Use the WSL 2 based engine*. |
| `port is already allocated` (8080 or 8090) | Another program uses the port. Stop it, or set `GATEWAY_PORT` / `CONTROLLER_PORT` in a `.env` file (see `.env.example`, Phase 2). |
| `exec /app/...: no such file or directory` or `\r` errors in containers | Windows line endings. The repo's `.gitattributes` forces LF. If you copied files manually, run `git add --renormalize .` in a Git checkout. |
| Containers stuck in `starting`/`unhealthy` | Run `docker compose logs <service>`. The health check output is in `docker inspect <container>` under `State.Health`. |
| Builds are slow the first time | Normal: the base image and Python packages are downloaded once and cached afterwards. |
| PowerShell blocks `verify.ps1` | Use `pwsh -ExecutionPolicy Bypass -File scripts\verify.ps1` (applies to this run only). |
