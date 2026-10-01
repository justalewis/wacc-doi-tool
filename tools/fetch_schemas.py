#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the Crossref XSDs the tool validates against, and everything they import.

    python tools/fetch_schemas.py

Starts from the two entry points, follows every schemaLocation / include / import
and the JATS entity references, and saves each file flat into schemas/. Remote
imports (w3.org's xml.xsd and the MathML 3 schemas) are saved beside the rest and
the references rewritten to the local copy, so validation never touches the network.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "schemas"
ENTRY = [
    "https://www.crossref.org/schemas/crossref5.4.0.xsd",
    "https://www.crossref.org/schemas/doi_resources4.3.6.xsd",
]
LOC = re.compile(r'(schemaLocation|SYSTEM)\s*=?\s*"([^"]+)"')
ENT = re.compile(r'<!ENTITY\s+%\s+\S+\s+(?:SYSTEM\s+)?"([^"]+\.(?:ent|dtd|mod|xsd))"')


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wac-doi-tool/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> int:
    OUT.mkdir(exist_ok=True)
    seen: dict[str, str] = {}
    queue = list(ENTRY)
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        name = url.rsplit("/", 1)[-1]
        try:
            data = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {url}: {e}", file=sys.stderr)
            seen[url] = name
            continue
        seen[url] = name
        text = data.decode("utf-8", errors="replace")
        for m in list(LOC.finditer(text)) + list(ENT.finditer(text)):
            ref = m.group(m.lastindex)
            absolute = urllib.parse.urljoin(url, ref)
            local = absolute.rsplit("/", 1)[-1]
            if absolute != ref:
                text = text.replace(f'"{ref}"', f'"{local}"')
            queue.append(absolute)
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"ok   {name}  ({len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
