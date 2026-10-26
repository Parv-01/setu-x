#!/usr/bin/env python3
"""Verify bibliography candidates against Crossref (and OpenAlex for abstracts).

Input : docs/research/reference_candidates.json  (list of {id, doi, expect:{title, first_author, year}})
Output: docs/research/doi_verification.json       (raw-ish metadata + match verdicts)

A candidate is VERIFIED only if the DOI resolves in Crossref AND the registered title,
first-author family name and year agree with what we expect. Anything else is flagged
for manual review. Uses only the standard library.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "docs" / "research" / "reference_candidates.json"
OUT = ROOT / "docs" / "research" / "doi_verification.json"
# Crossref asks for a contact address ("polite pool"). It is read from the environment so no
# personal address is stored in the repository: set SETU_CROSSREF_MAILTO=<your address>.
_MAILTO = os.environ.get("SETU_CROSSREF_MAILTO", "").strip()
UA = "setu-x-reference-check/0.1" + (f" (mailto:{_MAILTO})" if _MAILTO else "")


def fetch_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as exc:  # network errors are recorded, not hidden
        return {"_error": f"{type(exc).__name__}: {exc}"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def openalex_abstract(doi: str) -> str | None:
    d = fetch_json("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi))
    if not d or "_error" in d:
        return None
    inv = d.get("abstract_inverted_index")
    if not inv:
        return None
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def main() -> int:
    cands = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    results = []
    for c in cands:
        doi = c["doi"]
        msg = fetch_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
        rec = {"id": c["id"], "doi": doi, "expect": c["expect"]}
        if not msg or "_error" in msg or "message" not in msg:
            rec["verdict"] = "UNRESOLVED"
            rec["error"] = (msg or {}).get("_error", "no message")
            results.append(rec)
            continue
        m = msg["message"]
        title = html.unescape((m.get("title") or [""])[0])
        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() or a.get("name", "")
                   for a in m.get("author", [])]
        first_family = (m.get("author") or [{}])[0].get("family") or (m.get("author") or [{}])[0].get("name", "")
        year = None
        for key in ("published-print", "published-online", "issued", "published"):
            parts = (m.get(key) or {}).get("date-parts")
            if parts and parts[0] and parts[0][0]:
                year = parts[0][0]
                break
        venue = (m.get("container-title") or [""])[0] or m.get("publisher", "")
        abstract = m.get("abstract")
        if abstract:
            abstract = re.sub(r"<[^>]+>", " ", abstract)
            abstract = re.sub(r"\s+", " ", abstract).strip()
        else:
            abstract = openalex_abstract(doi)
        e = c["expect"]
        checks = {
            "title": norm(e["title"]) in norm(title) or norm(title) in norm(e["title"]),
            "first_author": norm(e["first_author"]) == norm(first_family),
            "year": int(e["year"]) == int(year) if year else False,
        }
        rec.update(
            {
                "registered": {
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "venue": venue,
                    "volume": m.get("volume"),
                    "issue": m.get("issue"),
                    "pages": m.get("page"),
                    "publisher": m.get("publisher"),
                    "type": m.get("type"),
                    "url": m.get("URL"),
                },
                "checks": checks,
                "verdict": "VERIFIED" if all(checks.values()) else "MISMATCH",
                "abstract": abstract,
                "abstract_source": "crossref" if m.get("abstract") else ("openalex" if abstract else None),
            }
        )
        results.append(rec)
        time.sleep(0.2)  # be polite to the APIs
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        flag = "" if r["verdict"] == "VERIFIED" else "  <-- review"
        reg = r.get("registered", {})
        title = reg.get("title", r.get("error", ""))[:80]
        has_abstract = "y" if r.get("abstract") else "n"
        print(f"{r['verdict']:<10} {r['id']:<22} {reg.get('year')} | {title} | abs={has_abstract}{flag}")
        if r["verdict"] == "MISMATCH":
            print(f"           checks={r['checks']} first_author_registered={reg.get('authors', ['?'])[:1]}")
    print("summary:", counts)
    return 0 if counts.get("VERIFIED", 0) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
