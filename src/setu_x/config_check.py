"""Validate a SETU-X config directory without starting any service.

Usage:  python -m setu_x.config_check [CONFIG_DIR]      (default: configs)
Exit 0 and a short summary if valid; exit 1 and the validation error otherwise.
"""
from __future__ import annotations

import sys

from setu_x.common.config import ConfigError
from setu_x.placement import load_config_dir


def main(argv: list[str]) -> int:
    config_dir = argv[1] if len(argv) > 1 else "configs"
    try:
        envs, placement = load_config_dir(config_dir)
    except ConfigError as exc:
        print(f"INVALID {exc}", file=sys.stderr)
        return 1
    simulated = [n for n, e in envs.environments.items() if e.simulated]
    print(f"VALID {config_dir}: {len(envs.environments)} environments ({len(simulated)} simulated: "
          f"{', '.join(simulated)}); call_mode={placement.call_mode}")
    for svc_name, svc in placement.service_items():
        inst = ", ".join(f"{i.name}@{i.env}{'' if i.enabled else ' (disabled)'}[{i.role}]" for i in svc.instances)
        print(f"  {svc_name}: routing={svc.routing}; {inst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
