#!/usr/bin/env python3
"""Documentation integrity checks (used in Phase 1 acceptance and later in CI).

1. Every citation key used in docs/*.md as [key] or [key1; key2] exists in docs/references.bib.
2. Every relative Markdown link or image in docs/ and README.md points to an existing file.
3. Reports bibliography entries that no document cites (informational, not a failure).
Exit code 1 on any missing citation or broken link.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIB = ROOT / "docs" / "references.bib"

KEY_RE = re.compile(r"\[([a-z][a-z0-9]+(?:\d{4}[a-z]*)?(?:;\s*[a-z][a-z0-9]+)*)\]")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def bib_keys() -> set[str]:
    return set(re.findall(r"^@\w+\{([^,]+),", BIB.read_text(encoding="utf-8"), re.M))


def main() -> int:
    keys = bib_keys()
    md_files = sorted((ROOT / "docs").rglob("*.md")) + [p for p in [ROOT / "README.md"] if p.exists()]
    missing, broken, used = [], [], set()
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        # strip fenced code blocks so code like list[str] is not mistaken for citations
        text_nocode = re.sub(r"```.*?```", "", text, flags=re.S)
        for m in KEY_RE.finditer(text_nocode):
            for k in (s.strip() for s in m.group(1).split(";")):
                # citation keys in this project always end in a 4-digit year or are known doc keys
                if re.search(r"\d{4}[a-z]*$", k) or k in keys:
                    used.add(k)
                    if k not in keys:
                        missing.append(f"{md.relative_to(ROOT)}: [{k}]")
        for m in LINK_RE.finditer(text_nocode):
            target = m.group(1).split("#")[0]
            if not target or re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
                continue
            if not (md.parent / target).exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
    print(f"checked {len(md_files)} markdown files; {len(keys)} bib entries; {len(used)} distinct keys cited")
    for x in missing:
        print("MISSING CITATION", x)
    for x in broken:
        print("BROKEN LINK     ", x)
    uncited = sorted(keys - used)
    if uncited:
        print("uncited bib entries (info):", ", ".join(uncited))
    ok = not missing and not broken
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
