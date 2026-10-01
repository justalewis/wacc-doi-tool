# -*- coding: utf-8 -*-
"""Read-only lookups against the public Crossref REST API.

Two jobs: confirm that a DOI is really registered before a reference deposit is
generated for it, and warn when a DOI about to be (re)deposited already exists.
Nothing here writes to Crossref.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API = "https://api.crossref.org/works/"
MAILTO = os.environ.get("CROSSREF_MAILTO", "")
TIMEOUT = float(os.environ.get("WACDOI_API_TIMEOUT", "8"))

FOUND, MISSING, UNREACHABLE = "found", "missing", "unreachable"


@dataclass
class Lookup:
    doi: str
    status: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    container: str = ""
    year: str = ""
    ref_count: int = 0
    url: str = ""


def _text(s: str) -> str:
    """Crossref titles arrive with markup and HTML entities (Editors&rsquo; Column).
    Flask escapes on output, so decode here or the entity shows up literally."""
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def lookup(doi: str) -> Lookup:
    q = urllib.parse.quote(doi, safe="/")
    url = API + q + (f"?mailto={urllib.parse.quote(MAILTO)}" if MAILTO else "")
    ua = "wac-doi-tool/1.0" + (f" (mailto:{MAILTO})" if MAILTO else "")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            msg = json.load(r).get("message", {})
    except urllib.error.HTTPError as e:
        return Lookup(doi, MISSING if e.code == 404 else UNREACHABLE)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return Lookup(doi, UNREACHABLE)

    authors = [_text(" ".join(filter(None, [a.get("given"), a.get("family") or a.get("name")])))
               for a in (msg.get("author") or msg.get("editor") or [])]
    issued = (msg.get("issued", {}).get("date-parts") or [[None]])[0]
    return Lookup(
        doi=doi,
        status=FOUND,
        title=_text((msg.get("title") or [""])[0]),
        authors=authors,
        container=_text((msg.get("container-title") or [""])[0]),
        year=str(issued[0]) if issued and issued[0] else "",
        ref_count=int(msg.get("reference-count") or 0),
        url=(msg.get("resource", {}).get("primary", {}) or {}).get("URL", ""),
    )
