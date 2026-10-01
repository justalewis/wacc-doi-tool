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


# -- step one, books: monograph or edited collection ------------------------

@dataclass
class Series:
    title: str
    issn_print: str = ""
    issn_online: str = ""


@dataclass
class Book:
    book_type: str                 # "edited_book" or "monograph"
    title: str
    doi: str
    url: str
    year: str
    people: list[Author] = field(default_factory=list)   # editors, or a monograph's authors
    subtitle: str = ""
    month: str = ""
    day: str = ""
    edition: str = ""
    isbn_print: str = ""
    isbn_online: str = ""
    publisher: str = "WAC Clearinghouse"
    place: str = ""
    language: str = "en"
    series: Series | None = None


@dataclass
class Chapter:
    title: str
    doi: str
    url: str
    authors: list[Author] = field(default_factory=list)
    subtitle: str = ""
    first_page: str = ""
    last_page: str = ""


def isbn_ok(raw: str) -> bool:
    """ISBN-10 or ISBN-13 with a correct check digit; hyphens and spaces ignored."""
    d = re.sub(r"[-\s]", "", raw or "")
    if re.fullmatch(r"\d{13}", d):
        total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d[:12]))
        return (10 - total % 10) % 10 == int(d[12])
    if re.fullmatch(r"\d{9}[\dX]", d):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
        return total % 11 == 0
    return False


def _contributors(parent, people: list[Author], role: str):
    c = _sub(parent, META_NS, "contributors")
    for i, au in enumerate(people):
        p = _sub(c, META_NS, "person_name", contributor_role=role,
                 sequence="first" if i == 0 else "additional")
        if au.given:
            _sub(p, META_NS, "given_name", au.given)
        _sub(p, META_NS, "surname", au.surname)
        if au.orcid:
            _sub(p, META_NS, "ORCID", f"https://orcid.org/{au.orcid}")


def build_book(dep: Depositor, book: Book, chapters: list[Chapter],
               batch_id: str, stamp: str) -> bytes:
    """A <book> deposit. An edited collection carries the volume's own DOI plus one
    content_item per chapter; a monograph carries its own DOI and optionally chapters."""
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

    b = _sub(_sub(root, N, "body"), N, "book", book_type=book.book_type)
    role = "editor" if book.book_type == "edited_book" else "author"

    def titles(parent, title, subtitle):
        t = _sub(parent, N, "titles")
        _sub(t, N, "title", title)
        if subtitle:
            _sub(t, N, "subtitle", subtitle)

    def doi_data(parent, doi, url):
        dd = _sub(parent, N, "doi_data")
        _sub(dd, N, "doi", doi)
        _sub(dd, N, "resource", url)

    if book.series:
        bm = _sub(b, N, "book_series_metadata", language=book.language)
        sm = _sub(bm, N, "series_metadata")
        _sub(_sub(sm, N, "titles"), N, "title", book.series.title)
        if book.series.issn_print:
            _sub(sm, N, "issn", book.series.issn_print, media_type="print")
        if book.series.issn_online:
            _sub(sm, N, "issn", book.series.issn_online, media_type="electronic")
    else:
        bm = _sub(b, N, "book_metadata", language=book.language)
    if book.people:
        _contributors(bm, book.people, role)
    titles(bm, book.title, book.subtitle)
    if book.edition:
        _sub(bm, N, "edition_number", book.edition)
    pd = _sub(bm, N, "publication_date", media_type="online")
    if book.month:
        _sub(pd, N, "month", f"{int(book.month):02d}")
    if book.day:
        _sub(pd, N, "day", f"{int(book.day):02d}")
    _sub(pd, N, "year", book.year)
    if book.isbn_print or book.isbn_online:
        if book.isbn_print:
            _sub(bm, N, "isbn", book.isbn_print, media_type="print")
        if book.isbn_online:
            _sub(bm, N, "isbn", book.isbn_online, media_type="electronic")
    else:
        _sub(bm, N, "noisbn", reason="monograph")
    pub = _sub(bm, N, "publisher")
    _sub(pub, N, "publisher_name", book.publisher)
    if book.place:
        _sub(pub, N, "publisher_place", book.place)
    doi_data(bm, book.doi, book.url)

    for ch in chapters:
        ci = _sub(b, N, "content_item", component_type="chapter", level_sequence_number="1",
                  publication_type="full_text", language=book.language)
        if ch.authors:
            _contributors(ci, ch.authors, "author")
        titles(ci, ch.title, ch.subtitle)
        if ch.first_page:
            pg = _sub(ci, N, "pages")
            _sub(pg, N, "first_page", ch.first_page)
            if ch.last_page:
                _sub(pg, N, "last_page", ch.last_page)
        doi_data(ci, ch.doi, ch.url)

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
