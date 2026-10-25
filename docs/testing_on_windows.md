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

## 2. Start, inspect and stop the stack (Phase 2)

```powershell
docker compose config            # validates compose.yaml; prints the resolved file
docker compose up --build -d     # builds images and starts all services in the background
docker compose ps                # every service should show "running (healthy)"
docker compose logs -f gateway   # follow one service's logs (Ctrl+C to stop following)
docker compose down              # stop and remove containers (images stay cached)
```

In VS Code you can see the same thing in the **Container Tools** side bar. Right-click a container for **View Logs** or **Attach Shell**.

## 3. Manual smoke test in the browser (Phase 2: API; Phase 6: dashboard)

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
docker compose --profile test run --rm tests                    # unit + contract + integration
docker compose --profile test run --rm tests pytest -m e2e      # end-to-end against the running stack
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
2. Open the **Testing** panel (flask icon). The repo's `.vscode/settings.json` (added in Phase 2) points it at pytest and `tests/`.
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
