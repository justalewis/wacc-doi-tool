# -*- coding: utf-8 -*-
"""Build and validate the two Crossref deposit files.

Step one is a metadata deposit (schema 5.4.0): journal, issue, articles. Step two is
a resources-only deposit (doi_resources 4.3.6) that binds a citation list to DOIs
that already exist. Both are validated against Crossref's own XSDs, saved in
schemas/ by tools/fetch_schemas.py, before anything is offered for download.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from lxml import etree

SCHEMAS = Path(__file__).parent / "schemas"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

META_NS = "http://www.crossref.org/schema/5.4.0"
META_XSD = "crossref5.4.0.xsd"
META_LOC = "https://www.crossref.org/schemas/crossref5.4.0.xsd"

REF_NS = "http://www.crossref.org/doi_resources_schema/4.3.6"
REF_XSD = "doi_resources4.3.6.xsd"

# Crossref narrowed DOI suffixes in 2008 to these characters. Older DOIs outside the
# set still work, but nothing new should use them.
DOI_RE = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9\-._;()/]+$")
ORCID_RE = re.compile(r"^(\d{4})-(\d{4})-(\d{4})-(\d{3}[\dX])$")


# -- small helpers ---------------------------------------------------------

def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def normalize_doi(raw: str) -> str:
    """Strip resolver prefixes and whitespace. DOIs compare case-insensitively, but
    the form a member deposited is the form that should go back out, so case is kept."""
    d = (raw or "").strip()
    d = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I)
    return d.strip()


def orcid_ok(value: str) -> bool:
    """ISO 7064 mod 11-2 check, the one ORCID uses for its final character."""
    m = ORCID_RE.match(value)
    if not m:
        return False
    digits = "".join(m.groups())
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return digits[-1] == ("X" if check == 10 else str(check))


@lru_cache(maxsize=None)
def _schema(name: str) -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(SCHEMAS / name)))


def schemas_available() -> bool:
    return (SCHEMAS / META_XSD).exists() and (SCHEMAS / REF_XSD).exists()


def validate(xml_bytes: bytes, kind: str) -> list[str]:
    """Return a list of schema errors; empty means the file is valid.
    kind is 'metadata' or 'references'."""
    xsd = META_XSD if kind == "metadata" else REF_XSD
    try:
        doc = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        return [f"Not well-formed XML: {e}"]
    schema = _schema(xsd)
    if schema.validate(doc):
        return []
    return [f"line {e.line}: {e.message}" for e in schema.error_log][:20]


def _serialise(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _sub(parent, ns: str, tag: str, text: str | None = None, **attrs):
    el = etree.SubElement(parent, f"{{{ns}}}{tag}", **attrs)
    if text is not None:
        el.text = text
    return el


# -- step one: metadata ----------------------------------------------------

@dataclass
class Depositor:
    name: str
    email: str
    registrant: str


@dataclass
class Journal:
    title: str
    abbrev: str = ""
    issn_print: str = ""
    issn_online: str = ""
    language: str = "en"


@dataclass
class Author:
    surname: str
    given: str = ""
    orcid: str = ""


@dataclass
class Article:
    title: str
    doi: str
    url: str
    authors: list[Author] = field(default_factory=list)
    subtitle: str = ""
    first_page: str = ""
    last_page: str = ""


@dataclass
class Issue:
    year: str
    volume: str = ""
    issue: str = ""
    month: str = ""
    day: str = ""


def build_metadata(dep: Depositor, journal: Journal, issue: Issue,
                   articles: list[Article], batch_id: str, stamp: str) -> bytes:
    N = META_NS
    root = etree.Element(
        f"{{{N}}}doi_batch", nsmap={None: N, "xsi": XSI}, version="5.4.0",
        attrib={f"{{{XSI}}}schemaLocation": f"{N} {META_LOC}"})

    head = _sub(root, N, "head")
    _sub(head, N, "doi_batch_id", batch_id)
    _sub(head, N, "timestamp", stamp)
    d = _sub(head, N, "depositor")
    _sub(d, N, "depositor_name", dep.name)
    _sub(d, N, "email_address", dep.email)
    _sub(head, N, "registrant", dep.registrant)

    body = _sub(root, N, "body")
    j = _sub(body, N, "journal")

    jm = _sub(j, N, "journal_metadata", language=journal.language)
    _sub(jm, N, "full_title", journal.title)
    if journal.abbrev:
        _sub(jm, N, "abbrev_title", journal.abbrev)
    if journal.issn_print:
        _sub(jm, N, "issn", journal.issn_print, media_type="print")
    if journal.issn_online:
        _sub(jm, N, "issn", journal.issn_online, media_type="electronic")

    def pub_date(parent):
        pd = _sub(parent, N, "publication_date", media_type="online")
        if issue.month:
            _sub(pd, N, "month", f"{int(issue.month):02d}")
        if issue.day:
            _sub(pd, N, "day", f"{int(issue.day):02d}")
        _sub(pd, N, "year", issue.year)

    ji = _sub(j, N, "journal_issue")
    pub_date(ji)
    if issue.volume:
        _sub(_sub(ji, N, "journal_volume"), N, "volume", issue.volume)
    if issue.issue:
        _sub(ji, N, "issue", issue.issue)

    for a in articles:
        ja = _sub(j, N, "journal_article", publication_type="full_text",
                  language=journal.language)
        t = _sub(ja, N, "titles")
        _sub(t, N, "title", a.title)
        if a.subtitle:
            _sub(t, N, "subtitle", a.subtitle)
        if a.authors:
            c = _sub(ja, N, "contributors")
            for i, au in enumerate(a.authors):
                p = _sub(c, N, "person_name", contributor_role="author",
                         sequence="first" if i == 0 else "additional")
                if au.given:
                    _sub(p, N, "given_name", au.given)
                _sub(p, N, "surname", au.surname)
                if au.orcid:
                    _sub(p, N, "ORCID", f"https://orcid.org/{au.orcid}")
        pub_date(ja)
        if a.first_page:
            pg = _sub(ja, N, "pages")
            _sub(pg, N, "first_page", a.first_page)
            if a.last_page:
                _sub(pg, N, "last_page", a.last_page)
        dd = _sub(ja, N, "doi_data")
        _sub(dd, N, "doi", a.doi)
        _sub(dd, N, "resource", a.url)

    return _serialise(root)


# -- step two: references --------------------------------------------------

@dataclass
class Reference:
    text: str
    doi: str = ""   # present only when the pasted entry itself carried one


def build_references(dep_name: str, dep_email: str,
                     items: list[tuple[str, list[Reference]]], batch_id: str) -> bytes:
    """One doi_citations block per DOI. A deposit replaces whatever list the DOI
    already carries, so every call must pass the complete list."""
    N = REF_NS
    root = etree.Element(
        f"{{{N}}}doi_batch", nsmap={None: N, "xsi": XSI}, version="4.3.6")
    head = _sub(root, N, "head")
    _sub(head, N, "doi_batch_id", batch_id)
    d = _sub(head, N, "depositor")
    _sub(d, N, "depositor_name", dep_name)
    _sub(d, N, "email_address", dep_email)
    body = _sub(root, N, "body")
    for doi, refs in items:
        dc = _sub(body, N, "doi_citations")
        _sub(dc, N, "doi", doi)
        cl = _sub(dc, N, "citation_list")
        for i, r in enumerate(refs, 1):
            c = _sub(cl, N, "citation", key=f"ref{i}")
            if r.doi:
                _sub(c, N, "doi", r.doi)
            _sub(c, N, "unstructured_citation", r.text)
    return _serialise(root)
