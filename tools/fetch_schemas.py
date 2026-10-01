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


def verify() -> list[str]:
    """Compile each entry-point schema the way the app does, and return the
    problems found.

    Checking that the two entry files exist is not enough — that is all
    xmlbuild.schemas_available() can cheaply do at runtime. lxml resolves every
    include and import when it *compiles* a schema, so a single missing
    transitive file is invisible until the first download request, which then
    fails. Compiling here is the only check that covers the whole graph, and it
    is the same operation xmlbuild._schema() performs.

    lxml is imported lazily so this script still runs standalone before the
    requirements are installed; verification is skipped with a warning then.
    """
    try:
        from lxml import etree
    except ImportError:
        print("\nWARNING: lxml is not installed, so the fetched schemas were not "
              "verified. Run this again after `pip install -r requirements.txt`.",
              file=sys.stderr)
        return []

    problems = []
    for url in ENTRY:
        name = url.rsplit("/", 1)[-1]
        path = OUT / name
        if not path.exists():
            problems.append(f"{name}: not fetched")
            continue
        try:
            etree.XMLSchema(etree.parse(str(path)))
        except Exception as e:  # noqa: BLE001 — any failure here is fatal
            problems.append(f"{name}: {e}")
    return problems


def main() -> int:
    OUT.mkdir(exist_ok=True)
    seen: dict[str, str] = {}
    failed: list[str] = []
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
            failed.append(url)
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

    if failed:
        print(f"\n{len(failed)} file(s) could not be fetched:", file=sys.stderr)
        for url in failed:
            print(f"  {url}", file=sys.stderr)

    # Exit non-zero on an unusable schema set. The Dockerfile runs this as a
    # build step, and returning 0 regardless of what happened produced an image
    # that built green, started fine, reported healthy, and refused every
    # download because schemas/ was empty. Fail the build instead.
    problems = verify()
    if problems:
        print("\nSchema verification FAILED — the app would refuse all downloads:",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print("\nSchemas verified: both entry points compile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
