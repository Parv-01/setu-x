# Release checklist (GitHub)

Nothing in this repository has been pushed anywhere. Publishing needs Parv's explicit go-ahead and his own GitHub account. This file lists what has to be true first, what is still open, and the exact commands.

## 1. Decisions only Parv can make

| Decision | Status | Notes |
|---|---|---|
| **License** | **Open** | Without a license file, nobody may legally reuse the code (all rights reserved). Common choices: **MIT** (short, permissive), **Apache-2.0** (permissive, with an explicit patent grant), or **CC BY 4.0 for docs and results + MIT/Apache for code** (often used for research artefacts). If the work counts as IIT Patna work, check the institute's IP policy first. |
| **Repository name and owner** | Open | `setu-x` is the working name. Whether it is free on GitHub, or anywhere else, has **not** been verified. Projects with similar names exist (for example "Setu"). Check under your own account; nothing here claims the name is available or safe as a trademark. |
| **Public or private** | Open | The repository contains machine details (CPU model, Windows build, Docker version) in `results/campaign_*.json` and the manifests. Personal details found by the §2 review are listed in the two rows below. |
| **E-mail in commit history** | Open | All commits are authored as `Parv Agarwal <parv.nlpiitp@gmail.com>`, and an older version of `scripts/verify_references.py` contained the same address (now read from `SETU_CROSSREF_MAILTO`). Pushing publishes that address. Options: accept it; or, before the first push, rewrite history to a GitHub `noreply` address (changes every commit hash; the commit `bfe122c` recorded in the Phase 8 results would then no longer exist under that name, so this needs a note in `docs/findings.md`). |
| **Windows user name in evidence** | Noted | `docs/evidence/verify_windows.log` shows temp paths under `C:\Users\HP\…`. That is a generic account name. The log is kept unedited because it is evidence. |
| **Affiliation wording** | Set | README: "Independent mini-project, not an officially endorsed IIT Patna project." Keep it if the repository is public. |

## 2. Pre-push checks (run from the repository root)

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Working tree clean, on `main` | `git status` | nothing to commit |
| 2 | No secrets, credential files or unvalidated results | `python scripts/repo_check.py` | `PASS` |
| 3 | Lint, tests, JS tests, docs | `ruff check src tests scripts`; `python -m pytest -m "not e2e and not browser"`; `node --test dashboard/tests/*.test.mjs`; `python scripts/check_docs.py` | all pass |
| 4 | Full verification from a clean clone (Windows) | `pwsh -File scripts/verify.ps1` | 18/18 PASS |
| 5 | Large files | `git ls-files -z \| xargs -0 du -k \| sort -n \| tail` | every file well below GitHub's 100 MB limit (largest ≈ 12 MB) |
| 6 | Author identity in commits | `git log --format='%an <%ae>' \| sort -u` | only identities you are happy to publish |
| 7 | No personal paths or names in tracked text | `git grep -n -i -E "c:\\\\users\\\\\|/home/[a-z]+/\|/Users/"` | no hits that reveal a user name |
| 8 | Screenshots | look at every file in `docs/evidence/screenshots/` | no credentials, tokens, e-mail addresses or other personal information |

## 3. What CI does (`.github/workflows/ci.yml`)

- **checks** (every push to `main` and every pull request): repository hygiene scan, ruff, config validation (good configs pass, bad fixtures fail), unit/contract/integration tests, dashboard JS tests, docs and bibliography check.
- **stack**: builds the images, starts the Compose stack with health checks, asserts every published port is bound to loopback, runs the e2e tests, prints the logs on failure, and tears down.
- **Browser tests**: only on a manual run with `browser: true`, because the Playwright image is about 3.5 GB.
- **Security posture:** read-only token (`permissions: contents: read`); checkout does not persist credentials; actions pinned to commit SHAs; no repository secrets are used. Dependabot (`.github/dependabot.yml`) proposes weekly updates to actions, pip pins and base images as pull requests.

Executed in the build workspace: every `checks` step, in a fresh Python 3.11 venv. Results: hygiene PASS, ruff clean, 337 passed / 1 skipped, JS 7/7, docs PASS. Also executed from the `stack` job: the port assertion (passes on the real stack, fails on a planted `0.0.0.0` binding) and the e2e tests (10 passed). `actionlint` reports no issues. **Not yet executed on GitHub itself:** the first real run happens after the first push.

## 4. Publishing (only after §1 is decided and §2 passes)

```powershell
# 1. Create an EMPTY repository on github.com under your account (no README, no license, no .gitignore).
# 2. Then, from E:\Projects\Cloud_Mini_Project\setu-x:
git remote add origin https://github.com/<your-user>/<repo-name>.git
git push -u origin main
# 3. On GitHub: Settings → Code security → enable "Private vulnerability reporting" and "Secret scanning".
# 4. Watch the first CI run under the "Actions" tab; fix anything that fails before tagging.
# 5. Optional tag once CI is green:
git tag -a v0.8.0 -m "SETU-X Phase 8: experiment campaign and findings"
git push origin v0.8.0
```

After the push, add the URL to `CITATION.cff` (`repository-code:`) and the README.
