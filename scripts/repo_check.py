"""Repository hygiene check (CI and local): the same rules as verify.ps1 V2.

Fails (exit 1) if a tracked file contains a secret-like pattern, a credential-like file is tracked,
or a committed results/<id>/ directory is incomplete or not validated. Run from the repository root:

    python scripts/repo_check.py
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

# Patterns are split so this file does not match itself.
SECRET_PATTERNS = [re.compile(p) for p in (
    "-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----",
    "AKIA" + r"[0-9A-Z]{16}",
    "ASIA" + r"[0-9A-Z]{16}",
    "gh" + r"p_[A-Za-z0-9]{36}",
    "github" + r"_pat_[A-Za-z0-9_]{20,}",
    "xox" + r"[baprs]-[A-Za-z0-9-]{10,}",
    "AI" + r"za[0-9A-Za-z_-]{35}",
    "sk-" + r"[A-Za-z0-9]{32,}",
    r"(?i)(aws_secret_access_key|client_secret|password|api_key)" + r"""\s*[:=]\s*['"][^'"\s]{8,}['"]""",
)]
CREDENTIAL_FILE = re.compile(r"(^|/)\.env$|\.pem$|\.key$|(^|/)id_(rsa|ed25519)$|credentials.*\.json$")
BINARY = re.compile(r"\.(png|jpe?g|gif|ico|pdf|pptx|woff2?)$", re.IGNORECASE)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
    return [p for p in out.decode().split("\0") if p]


def secret_hits(files: list[str]) -> list[str]:
    hits = []
    for f in files:
        if BINARY.search(f) or not Path(f).is_file():
            continue
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            hits.append(f"{f}: unreadable ({exc})")
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in SECRET_PATTERNS):
                hits.append(f"{f}:{n}")
    return hits


def result_problems(files: list[str]) -> tuple[list[str], int]:
    dirs = sorted({f.split("/")[1] for f in files if f.startswith("results/") and f.count("/") >= 2})
    problems = []
    for d in dirs:
        base = Path("results") / d
        if d == "analysis":
            continue
        need = [base / "summary.json", base / "manifest.json", base / "observations.csv"]
        if not all(p.is_file() for p in need):
            problems.append(f"{d}: incomplete")
            continue
        summary = json.loads(need[0].read_text(encoding="utf-8"))
        with need[2].open(encoding="utf-8", newline="") as fh:
            rows = sum(1 for _ in csv.DictReader(fh))
        validation = summary.get("validation") or {}
        if not validation.get("ok") or rows != validation.get("rows_csv"):
            problems.append(f"{d}: not validated or row count differs ({rows} vs {validation.get('rows_csv')})")
    return problems, len([d for d in dirs if d != "analysis"])


def main() -> int:
    files = tracked_files()
    hits = secret_hits(files)
    bad = [f for f in files if CREDENTIAL_FILE.search(f)]
    problems, n_results = result_problems(files)
    for h in hits:
        print(f"possible secret: {h}")
    for b in bad:
        print(f"credential-like file tracked: {b}")
    for p in problems:
        print(f"results problem: {p}")
    ok = not (hits or bad or problems)
    print(f"{'PASS' if ok else 'FAIL'}: scanned {len(files)} tracked files; "
          f"{n_results} committed result directories checked")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
