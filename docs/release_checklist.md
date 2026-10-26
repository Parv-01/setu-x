# Release checklist (GitHub)

Publishing was authorised by Parv on 2026-10-26. This file records the decisions, the pre-push checks and how the repository was published.

## 1. Decisions (made by Parv, 2026-10-26)

| Decision | Outcome |
|---|---|
| License | Apache-2.0 for code (`LICENSE`, `NOTICE`); CC BY 4.0 for documentation, figures, presentation and results (`LICENSES/CC-BY-4.0.txt`). Texts copied unmodified from the SPDX license list (spdx/license-list-data, commit `31ba1a5`) |
| Repository | `https://github.com/Parv-01/setu-x`, created by Parv. This confirms only that the GitHub name was free under his account; it is not a trademark check |
| E-mail in commit history | Accepted as is. Commits from 2026-10-26 onwards use parvagarwal9759@gmail.com |
| Windows user name in evidence | `docs/evidence/verify_windows.log` shows `C:\Users\HP\…` (a generic account name); kept unedited as evidence |
| Affiliation wording | "Independent mini-project, not an officially endorsed IIT Patna project." |
| Push | Authorised by Parv on 2026-10-26 |

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
