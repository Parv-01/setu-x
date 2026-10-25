"""Command line for the experiment harness.

    python -m setu_x.experiments.cli validate configs/scenarios/*.yaml
    python -m setu_x.experiments.cli run --scenario configs/scenarios/smoke.yaml [--git-commit SHA]
    python -m setu_x.experiments.cli analyse results/<experiment_id>

`run` should be executed INSIDE the Compose network so the generator shares the services' clock
domain:   docker compose exec runner python -m setu_x.experiments.cli run --scenario ...
Running it on the host works (pass --controller/--gateway URLs) but the manifest then records a
warning, because host and container clocks differ (docs/methodology.md §2).
Exit codes: 0 ok, 1 experiment failed or records invalid, 2 bad input.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from setu_x.common.config import ConfigError
from setu_x.experiments.runner import ControlPlane, ExperimentError, ExperimentRunner, analyse_records
from setu_x.experiments.scenario import Scenario, load_scenario
from setu_x.placement import load_config_dir


def _validate(args: argparse.Namespace) -> int:
    envs, base = load_config_dir(args.config_dir)
    bad = 0
    for path in args.files:
        try:
            sc, _ = load_scenario(path, envs, base)
            w = sc.workload
            print(f"OK    {path}: {sc.id} ({sc.requires_mode}), {len(sc.conditions)} conditions x {sc.runs} runs, "
                  f"{w.rate_rps} req/s, {w.warmup_s}+{w.duration_s} s per run")
        except ConfigError as exc:
            bad += 1
            print(f"ERROR {exc}")
    return 2 if bad else 0


async def _run(args: argparse.Namespace) -> int:
    envs, base = load_config_dir(args.config_dir)
    path = Path(args.scenario)
    sc, _ = load_scenario(path, envs, base)
    async with httpx.AsyncClient(trust_env=False) as http, httpx.AsyncClient(
            trust_env=False, limits=httpx.Limits(max_connections=300, max_keepalive_connections=300)) as load:
        runner = ExperimentRunner(sc, path.read_text(encoding="utf-8"), str(path).replace("\\", "/"), envs, base,
                                  ControlPlane(http, args.controller, args.gateway), load, Path(args.results),
                                  git_commit=args.git_commit, label=args.label)
        if not os.environ.get("SETU_INSTANCE"):
            runner.warnings.append("run from outside the runner container: the load generator may be in a "
                                   "different clock domain than the services")
        print(f"experiment {runner.id}: {sc.total_runs()} runs -> {runner.dir}")
        try:
            summary = await runner.run()
        except ExperimentError as exc:
            print(f"FAILED before start: {exc}", file=sys.stderr)
            return 1
    v = summary.get("validation", {})
    print(f"status={runner.status} runs={summary.get('runs_completed')}/{summary.get('runs_planned')} "
          f"validation={'ok' if v.get('ok') else 'PROBLEMS'}")
    for problem in v.get("problems", []):
        print(f"  problem: {problem}")
    if runner.error:
        print(f"  error: {runner.error}", file=sys.stderr)
    return 0 if runner.status == "completed" and v.get("ok") else 1


def _analyse(args: argparse.Namespace) -> int:
    d = Path(args.directory)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    sc = Scenario.model_validate(manifest["scenario"])
    summary = analyse_records(d, sc, manifest.get("runs", []), {
        "experiment_id": manifest.get("experiment_id"), "status": manifest.get("status"),
        "git_commit": manifest.get("git_commit"), "warnings": manifest.get("warnings", [])})
    v = summary["validation"]
    print(f"{d.name}: validation={'ok' if v.get('ok') else 'PROBLEMS'} rows={v.get('rows_jsonl')}")
    for problem in v.get("problems", []):
        print(f"  problem: {problem}")
    return 0 if v.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="setu_x.experiments.cli")
    p.add_argument("--config-dir", default=os.environ.get("SETU_CONFIG_DIR", "configs"))
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate scenario files")
    v.add_argument("files", nargs="+")
    r = sub.add_parser("run", help="run one scenario")
    r.add_argument("--scenario", required=True)
    r.add_argument("--controller", default=os.environ.get("SETU_CONTROLLER_URL", "http://controller:8000"))
    r.add_argument("--gateway", default=os.environ.get("SETU_GATEWAY_URL", "http://gateway:8000"))
    r.add_argument("--results", default=os.environ.get("SETU_RESULTS_DIR", "results"))
    r.add_argument("--git-commit", default=None)
    r.add_argument("--label", default="")
    a = sub.add_parser("analyse", help="re-validate and re-summarise a results directory")
    a.add_argument("directory")
    args = p.parse_args(argv)
    try:
        if args.cmd == "validate":
            return _validate(args)
        if args.cmd == "run":
            return asyncio.run(_run(args))
        return _analyse(args)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
