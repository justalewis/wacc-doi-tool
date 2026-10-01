# -*- coding: utf-8 -*-
import html
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402
import crossref_api  # noqa: E402
import xmlbuild as xb  # noqa: E402

pytestmark = pytest.mark.skipif(not xb.schemas_available(), reason="run tools/fetch_schemas.py")


@pytest.fixture
def client(monkeypatch):
    # No test may touch the network. Default: Crossref has never heard of any DOI.
    monkeypatch.setattr(crossref_api, "lookup",
                        lambda doi: crossref_api.Lookup(doi, crossref_api.MISSING))
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


BASE = {
    "dep_name": "Jane Staff", "dep_email": "staff@example.org", "registrant": "WAC Clearinghouse",
    "journal": "wac", "year": "2026", "volume": "37", "issue": "1", "month": "3",
    "mode": "issue", "start_no": "1", "action": "build",
}


def form(**over):
    d = dict(BASE)
    d.update(over)
    return d


def post_meta(client, rows, **over):
    data = form(**over)
    for key in ("title", "subtitle", "authors", "first", "last", "doi", "url"):
        data[f"a_{key}"] = [r.get(key, "") for r in rows]
    return client.post("/metadata", data=data)


ROW = {"title": "On Writing", "authors": "Smith, Jane | 0000-0002-1825-0097\nOkafor, Chidi",
       "first": "1", "last": "12", "url": "https://wacclearinghouse.org/wacj/v37/1.pdf"}


def test_pages_render(client):
    for path in ("/", "/metadata", "/metadata?mode=article", "/references", "/health"):
        assert client.get(path).status_code == 200


def test_issue_mode_generates_valid_file_and_generated_dois(client):
    r = post_meta(client, [ROW, dict(ROW, title="Second")])
    body = r.get_data(as_text=True)
    assert "Valid." in body
    assert "10.37514/wac-j.2026.37.1.01" in body and "10.37514/wac-j.2026.37.1.02" in body
    assert "was generated from the" in body  # the human is told these were not typed


def test_download_revalidates(client):
    r = post_meta(client, [ROW])
    assert b"Download" in r.data
    good = client.post("/metadata/download",
                       data={"xml": xb.build_metadata(
                           xb.Depositor("n", "a@b.org", "r"), xb.Journal("J", issn_online="1544-4929"),
                           xb.Issue("2026"), [xb.Article("T", "10.37514/x.1", "https://e.org")],
                           "wacc-test-1", "1").decode(), "filename": "ok.xml"})
    assert good.status_code == 200 and good.mimetype == "application/xml"
    bad = client.post("/metadata/download", data={"xml": "<doi_batch/>", "filename": "x.xml"})
    assert bad.status_code == 422


def test_wrong_prefix_and_bad_orcid_and_author_format_rejected(client):
    r = post_meta(client, [dict(ROW, doi="10.1234/other",
                                authors="Smith Jane\nLee, K | 0000-0000-0000-0000")])
    body = r.get_data(as_text=True)
    assert "must start with 10.37514/" in body
    assert "Surname, Given name" in body
    assert "fails its checksum" in body


def test_duplicate_dois_rejected(client):
    r = post_meta(client, [dict(ROW, doi="10.37514/a-j.1"), dict(ROW, doi="10.37514/a-j.1")])
    assert "appears twice" in r.get_data(as_text=True)


def test_already_registered_doi_is_flagged(client, monkeypatch):
    monkeypatch.setattr(crossref_api, "lookup", lambda doi: crossref_api.Lookup(
        doi, crossref_api.FOUND, title="Existing Piece"))
    r = post_meta(client, [ROW])
    assert "ALREADY REGISTERED" in r.get_data(as_text=True)


def test_add_row_keeps_what_was_typed(client):
    r = post_meta(client, [ROW], action="add_row")
    body = r.get_data(as_text=True)
    assert "On Writing" in body
    assert body.count('name="a_title"') == appmod.MIN_ROWS["issue"]  # 2 rows, padded to the minimum


# -- step two --------------------------------------------------------------

REFS = ("Works Cited\n\nAdler, Mortimer. How to Read a Book. Simon, 1940.\n\n"
        "Baker, Tom. Other Book. Pub, 2002. https://doi.org/10.1234/abc.1\n")


def refs_form(**over):
    d = {"dep_name": "Jane", "dep_email": "j@example.org", "doi": "10.37514/wac-j.2026.37.1.01",
         "text": REFS}
    d.update(over)
    return d


def test_references_blocked_until_doi_exists(client):
    r = client.post("/references", data=refs_form())
    body = r.get_data(as_text=True)
    assert "Crossref has no record of" in body
    assert "Review the entries" not in body


def test_references_flow_when_doi_exists(client, monkeypatch):
    monkeypatch.setattr(crossref_api, "lookup", lambda doi: crossref_api.Lookup(
        doi, crossref_api.FOUND, title="On Writing", ref_count=3))
    r = client.post("/references", data=refs_form())
    body = r.get_data(as_text=True)
    assert "2</strong> entries found" in body and "replaces</strong> the existing list" in body

    build = client.post("/references/build", data={
        "dep_name": "Jane", "dep_email": "j@example.org", "doi": "10.37514/wac-j.2026.37.1.01",
        "ref": ["Adler, Mortimer. How to Read a Book. Simon, 1940.",
                "Baker, Tom. Other Book. Pub, 2002. https://doi.org/10.1234/abc.1", "  "]})
    out = build.get_data(as_text=True)
    assert "Valid." in out and "2 references" in out
    assert "&lt;doi&gt;10.1234/abc.1&lt;/doi&gt;" in out


def test_references_wrong_prefix_refused(client):
    r = client.post("/references", data=refs_form(doi="10.9999/elsewhere"))
    assert "must start with 10.37514/" in r.get_data(as_text=True)


def test_references_override_when_crossref_unreachable(client, monkeypatch):
    monkeypatch.setattr(crossref_api, "lookup", lambda doi: crossref_api.Lookup(
        doi, crossref_api.UNREACHABLE))
    blocked = client.post("/references", data=refs_form())
    assert "Could not reach Crossref" in blocked.get_data(as_text=True)
    ok = client.post("/references", data=refs_form(override="1"))
    assert "entries found" in ok.get_data(as_text=True)


# -- the passphrase gate ---------------------------------------------------

@pytest.fixture
def gated(client, monkeypatch):
    monkeypatch.setattr(appmod, "ACCESS_CODE", "correct horse")
    appmod._fails.clear()
    return client


def test_gate_redirects_every_tool_page_and_download(gated):
    for path in ("/", "/metadata", "/references"):
        r = gated.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert gated.post("/metadata/download", data={"xml": "<a/>"}).status_code == 302
    assert gated.post("/references/build", data={}).status_code == 302


def test_health_and_static_stay_open(gated):
    assert gated.get("/health").status_code == 200
    assert gated.get("/static/style.css").status_code == 200


def test_wrong_passphrase_is_refused_right_one_lets_in(gated):
    assert gated.post("/login", data={"code": "nope"}).status_code == 401
    assert gated.get("/metadata").status_code == 302
    r = gated.post("/login?next=/references", data={"code": "correct horse"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/references")
    assert gated.get("/metadata").status_code == 200
    gated.post("/logout")
    assert gated.get("/metadata").status_code == 302


def test_login_will_not_redirect_off_site(gated):
    r = gated.post("/login?next=//evil.example", data={"code": "correct horse"})
    assert "evil.example" not in r.headers["Location"]


def test_lockout_after_repeated_failures(gated):
    for _ in range(appmod.LOGIN_MAX_FAILS):
        gated.post("/login", data={"code": "bad"})
    assert gated.post("/login", data={"code": "correct horse"}).status_code == 429


# -- books -----------------------------------------------------------------

def post_book(client, chapters, **over):
    data = {"dep_name": "Jane Staff", "dep_email": "staff@example.org",
            "registrant": "WAC Clearinghouse", "type": "edited", "b_title": "Collected Essays",
            "b_people": "Okafor, Chidi\nSmith, Jane", "b_doi": "10.37514/PER-B.2026.1234",
            "b_url": "https://wacclearinghouse.org/books/1234", "year": "2026",
            "isbn_online": "978-3-16-148410-0", "publisher": "WAC Clearinghouse",
            "start_no": "1", "action": "build"}
    data.update(over)
    for key in ("title", "subtitle", "authors", "first", "last", "doi", "url", "part"):
        data[f"a_{key}"] = [c.get(key, "") for c in chapters]
    return client.post("/book", data=data)


CH = {"title": "On Writing", "authors": "Lee, Ann", "first": "1", "last": "12",
      "url": "https://wacclearinghouse.org/books/1234/1.pdf"}


def test_mint_chooser_and_book_pages_render(client):
    body = client.get("/mint").get_data(as_text=True)
    for label in ("Journal article", "Edited collection", "Monograph"):
        assert label in body
    assert "Chapters" in client.get("/book?type=edited").get_data(as_text=True)
    assert "monograph" in client.get("/book?type=monograph").get_data(as_text=True)


def test_edited_collection_builds_with_generated_chapter_dois(client):
    r = post_book(client, [CH, dict(CH, title="Second")])
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Valid." in body
    assert "10.37514/PER-B.2026.1234.2.01" in body and "10.37514/PER-B.2026.1234.2.02" in body
    assert "Download" in body


def test_monograph_needs_no_chapters(client):
    r = post_book(client, [], type="monograph")
    assert "Valid." in r.get_data(as_text=True)


def test_edited_collection_needs_a_chapter_and_a_book_doi(client):
    body = post_book(client, [], b_doi="").get_data(as_text=True)
    assert "needs at least one chapter" in body and "book&#39;s own DOI is required" in body


def test_book_validation_errors(client):
    body = post_book(client, [CH], isbn_online="978-3-16-148410-1", series_title="S",
                     b_doi="10.9999/nope").get_data(as_text=True)
    assert "not a valid ISBN" in body and "needs at least one ISSN" in body
    assert "DOI must start with 10.37514/" in body


def test_book_download_revalidates(client):
    body = post_book(client, [CH]).get_data(as_text=True)
    xml = html.unescape(re.search(r'name="xml" value="([^"]*)"', body).group(1))
    assert client.post("/book/download", data={"xml": xml, "filename": "x.xml"}).status_code == 200
    bad = client.post("/book/download", data={"xml": "<nope/>", "filename": "x.xml"})
    assert bad.status_code == 422


def test_book_flags_already_registered_dois(client, monkeypatch):
    monkeypatch.setattr(crossref_api, "lookup", lambda doi: crossref_api.Lookup(
        doi, crossref_api.FOUND, title="Existing"))
    assert "ALREADY REGISTERED" in post_book(client, [CH]).get_data(as_text=True)


def test_book_doi_built_from_series_year_and_id(client):
    body = post_book(client, [CH, dict(CH, title="Intro", part="front"), dict(CH, title="Two")],
                     b_doi="", series_code="PER", book_id="1947", year="2023").get_data(as_text=True)
    assert "Valid." in body
    assert "10.37514/PER-B.2023.1947" in body
    assert "10.37514/PER-B.2023.1947.1.1" in body          # front matter
    assert "10.37514/PER-B.2023.1947.2.01" in body and "10.37514/PER-B.2023.1947.2.02" in body


def test_book_id_must_be_four_digits_and_series_known(client):
    body = post_book(client, [CH], b_doi="", series_code="PER", book_id="19").get_data(as_text=True)
    assert "four-digit number" in body and "four-digit book ID" in body
    assert "Unknown series code" in post_book(client, [CH], series_code="ZZZ").get_data(as_text=True)


def test_blank_part_rows_do_not_count_as_chapters(client):
    r = post_book(client, [dict(part="chapter")] * 3, type="edited")
    assert "needs at least one chapter" in r.get_data(as_text=True)
