# Security policy

SETU-X is a research prototype that runs on one machine. It is not a production service.

## Supported versions

Only the latest commit on `main` is maintained.

## Design assumptions

- All published ports are bound to `127.0.0.1`. The stack is not meant to be exposed to a network.
- The controller only accepts requests whose `Host` header is on an allow-list (against DNS rebinding). State-changing requests must come from the same origin or localhost.
- Fault injection is bounded: an allow-list of targets and types, a maximum duration, and automatic expiry. There is no unrestricted failure endpoint.
- Containers run as a non-root user with a read-only root filesystem, all capabilities dropped and `no-new-privileges`.
- Real-cloud operations are disabled by default and not implemented (`docs/cloud_setup.md`). The repository contains no credentials. CI (`scripts/repo_check.py`) fails if a tracked file looks like a secret or a credential file.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting ("Security" tab → "Report a vulnerability") on this repository. Include the commit, the steps to reproduce and the impact you expect.

This is a one-person research project, so there is no response-time commitment. Reports are read and answered as soon as possible.
