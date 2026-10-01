# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xmlbuild as xb  # noqa: E402
from refs import split_references  # noqa: E402

needs_xsd = pytest.mark.skipif(not xb.schemas_available(),
                               reason="run tools/fetch_schemas.py first")

DEP = xb.Depositor("WAC Clearinghouse", "doi@example.org", "WAC Clearinghouse")
JOURNAL = xb.Journal("The WAC Journal", abbrev="WAC J.", issn_print="1544-4929",
                     issn_online="2469-7788")
ISSUE = xb.Issue(year="2026", volume="37", issue="1", month="3")


def _article(n=1, **kw):
    base = dict(title="A & B <Test>", doi=f"10.37514/wac-j.2026.37.1.{n:02d}",
                url=f"https://wacclearinghouse.org/docs/journals/wacj/v37/{n}.pdf",
                authors=[xb.Author("Smith", "Jane", "0000-0002-1825-0097"),
                         xb.Author("Okafor", "Chidi")],
                first_page="1", last_page="14")
    base.update(kw)
    return xb.Article(**base)


# -- identifiers -----------------------------------------------------------

def test_orcid_checksum():
    assert xb.orcid_ok("0000-0002-1825-0097")
    assert xb.orcid_ok("0000-0002-9079-593X")
    assert not xb.orcid_ok("0000-0002-1825-0098")
    assert not xb.orcid_ok("not-an-orcid")


def test_normalize_doi():
    assert xb.normalize_doi(" https://doi.org/10.37514/x-j.1 ") == "10.37514/x-j.1"
    assert xb.normalize_doi("doi: 10.37514/x-j.1") == "10.37514/x-j.1"


# -- metadata deposit ------------------------------------------------------

@needs_xsd
def test_metadata_validates_and_escapes():
    xml = xb.build_metadata(DEP, JOURNAL, ISSUE, [_article(1), _article(2)],
                            "wacc-test-1", "20261001120000")
    assert xb.validate(xml, "metadata") == []
    assert b"A &amp; B &lt;Test&gt;" in xml


@needs_xsd
def test_metadata_minimal_article_validates():
    art = xb.Article(title="Bare", doi="10.37514/x-j.2026.1.1.01", url="https://example.org/1")
    xml = xb.build_metadata(DEP, xb.Journal("J"), xb.Issue(year="2026"), [art], "b-0001", "1")
    assert xb.validate(xml, "metadata") == []


@needs_xsd
def test_validation_catches_a_bad_file():
    xml = xb.build_metadata(DEP, JOURNAL, ISSUE, [_article()], "x", "1")  # batch id < 4 chars
    assert xb.validate(xml, "metadata")


# -- references deposit ----------------------------------------------------

@needs_xsd
def test_references_validate():
    refs = [xb.Reference("Smith, Jane. \"Title.\" Journal, 2020."),
            xb.Reference("Doe, J. (2019). Thing. https://doi.org/10.1234/abc", doi="10.1234/abc")]
    xml = xb.build_references("Name", "a@b.org", [("10.37514/wac-j.2026.37.1.01", refs)],
                              "wacc-refs-0001")
    assert xb.validate(xml, "references") == []
    assert b'key="ref1"' in xml and b'key="ref2"' in xml


@needs_xsd
def test_references_multiple_dois():
    xml = xb.build_references("N", "a@b.org", [
        ("10.37514/a-j.1", [xb.Reference("One reference long enough.")]),
        ("10.37514/a-j.2", [xb.Reference("Another reference long enough.")])], "wacc-refs-0002")
    assert xb.validate(xml, "references") == []


# -- reference splitting ---------------------------------------------------

def texts(s):
    return [e.text for e in split_references(s)]


def test_split_blank_lines_and_heading():
    s = "References\n\nAdler, M. (2001). One. Press.\n\nBaker, T. (2002). Two. Press.\n"
    assert texts(s) == ["Adler, M. (2001). One. Press.", "Baker, T. (2002). Two. Press."]


def test_split_one_per_line():
    s = "Works Cited\nAdler, Mortimer. How to Read. Simon, 1940.\nBaker, Tom. Other Book. Pub, 2002.\n"
    assert len(texts(s)) == 2


def test_split_hanging_indent():
    s = ("Adler, Mortimer. How to Read a Book: The Art of Getting\n"
         "    a Liberal Education. Simon, 1940.\n"
         "Baker, Tom. Other Book. Pub, 2002.\n")
    got = texts(s)
    assert len(got) == 2 and "Liberal Education" in got[0]


def test_split_wrapped_lines_without_indent():
    s = ("Adler, Mortimer. How to Read a Book: The Art of Getting\n"
         "a Liberal Education. Simon, 1940.\n"
         "Baker, Tom. Other Book. Pub, 2002.\n")
    assert len(texts(s)) == 2


def test_numbering_stripped_and_dois_extracted():
    s = "1. Smith, J. (2020). Thing. https://doi.org/10.1234/abc.def.\n2. Lee, K. (2021). Other. Press."
    es = split_references(s)
    assert es[0].text.startswith("Smith")
    assert es[0].doi == "10.1234/abc.def"
    assert es[1].doi == ""


def test_dash_author_and_duplicate_warn():
    s = ("Adler, M. How to Read. Simon, 1940.\n\n---. Second Book. Simon, 1950.\n\n"
         "Adler, M. How to Read. Simon, 1940.")
    es = split_references(s)
    assert any("dash" in w for w in es[1].warnings)
    assert any("Duplicate" in w for w in es[2].warnings)


def test_empty_input():
    assert split_references("  \n\n") == []


def test_crossref_titles_are_decoded():
    from crossref_api import _text
    assert _text("Editors&rsquo; Column") == "Editors\u2019 Column"
    assert _text("On <i>Writing</i> &amp; Rhetoric") == "On Writing & Rhetoric"
