#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the Crossref XSDs the tool validates against, and everything they import.

    python tools/fetch_schemas.py

Starts from the two entry points, follows every schemaLocation / include / import
and the JATS entity references, and saves the result into schemas/ so validation
never touches the network.

Files are saved flat, because xmlbuild looks them up by bare filename — but flat
is not the same as "by basename", and the difference matters. Two unrelated
documents can share a filename: Crossref's doi_resources chain and the JATS
chain each pull their own MathML 3 set, with the same five names and different
contents, and w3.org's xml.xsd arrives from three different directories. Writing
those by basename means the last one wins, which silently produced a schema set
that could not compile (`doi_resources4.3.6.xsd` importing a MathML schema that
did not declare the `math` element it references). Colliding files therefore get
a short digest of their source directory prefixed, and each referrer is rewritten
to the copy it actually asked for. The two entry points always keep their plain
names, since that is how xmlbuild asks for them.

Fetching and rewriting are separate passes: a reference cannot be pointed at its
local filename until every document has been fetched and had a name chosen,
because the target may not have been downloaded yet when the referrer is read.
"""
from __future__ import annotations

import hashlib
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


def references(url: str, text: str) -> list[tuple[str, str]]:
    """Every outgoing reference in *text*, as (reference as written, absolute URL)."""
    out = []
    for m in list(LOC.finditer(text)) + list(ENT.finditer(text)):
        ref = m.group(m.lastindex)
        out.append((ref, urllib.parse.urljoin(url, ref)))
    return out


def assign_names(urls: list[str]) -> dict[str, str]:
    """Pick a flat filename per URL, keeping colliding basenames apart.

    The entry points are claimed first and unconditionally: xmlbuild asks for
    `crossref5.4.0.xsd` and `doi_resources4.3.6.xsd` by those exact names, so
    they must never be the ones that get a digest. Everything else is processed
    in sorted order so the naming is deterministic run to run — the fetch queue
    is LIFO and its order is not.
    """
    names: dict[str, str] = {}
    claimed: set[str] = set()

    for url in ENTRY:
        if url in urls:
            base = url.rsplit("/", 1)[-1]
            names[url] = base
            claimed.add(base)

    for url in sorted(urls):
        if url in names:
            continue
        base = url.rsplit("/", 1)[-1]
        if base not in claimed:
            names[url] = base
            claimed.add(base)
            continue
        digest = hashlib.sha1(url.rsplit("/", 1)[0].encode()).hexdigest()[:8]
        names[url] = f"{digest}-{base}"
        claimed.add(names[url])

    return names


def verify() -> list[str]:
    """Compile each entry-point schema the way the app does, and return the
    problems found.

    Checking that the two entry files exist is not enough — that is all
    xmlbuild.schemas_available() can cheaply do at runtime. lxml resolves every
    include and import when it *compiles* a schema, so a single missing or
    mismatched transitive file is invisible until the first download request,
    which then fails. Compiling here is the only check that covers the whole
    graph, and it is the same operation xmlbuild._schema() performs.

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

    # Pass 1 — fetch every reachable document, keeping its text and its
    # outgoing references. Nothing is written yet: a reference cannot be
    # rewritten until the document it points at has a chosen filename.
    docs: dict[str, str] = {}
    refs: dict[str, list[tuple[str, str]]] = {}
    failed: list[str] = []

    queue = list(ENTRY)
    while queue:
        url = queue.pop()
        if url in docs or url in failed:
            continue
        try:
            data = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {url}: {e}", file=sys.stderr)
            failed.append(url)
            continue
        text = data.decode("utf-8", errors="replace")
        docs[url] = text
        refs[url] = references(url, text)
        queue.extend(target for _, target in refs[url])
        print(f"ok   {url.rsplit('/', 1)[-1]}  ({len(data):,} bytes)")

    if failed:
        print(f"\n{len(failed)} file(s) could not be fetched:", file=sys.stderr)
        for url in failed:
            print(f"  {url}", file=sys.stderr)

    # Pass 2 — now that every document has a name, point each reference at the
    # copy its own referrer asked for and write the files out. Absolute
    # references are rewritten too; leaving them alone sent validation to the
    # network at request time, which this script exists to avoid.
    names = assign_names(list(docs))
    renamed = {u: n for u, n in names.items() if n != u.rsplit("/", 1)[-1]}

    for url, text in docs.items():
        for ref, target in refs[url]:
            local = names.get(target)
            if local and ref != local:
                text = text.replace(f'"{ref}"', f'"{local}"')
        (OUT / names[url]).write_text(text, encoding="utf-8")

    print(f"\nwrote {len(docs)} file(s) to {OUT}")
    if renamed:
        print(f"{len(renamed)} had a colliding filename and were kept apart:")
        for url, name in sorted(renamed.items(), key=lambda kv: kv[1]):
            print(f"  {name}  <-  {url}")

    # Exit non-zero on an unusable schema set. The Dockerfile runs this as a
    # build step, and returning 0 regardless of what happened produced an image
    # that built green, started, reported healthy, and refused every download
    # because the schemas could not compile. Fail the build instead.
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
