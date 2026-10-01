# -*- coding: utf-8 -*-
"""WAC Clearinghouse DOI tool.

Two steps, in this order, and the second refuses to run until the first has happened:

  1. /metadata    form -> Crossref metadata deposit (.xml) for the Crossref admin uploader
  2. /references  paste a Works Cited -> reference deposit (.xml) bound to a DOI that
                  Crossref already knows about

The tool never talks to Crossref except to read: it looks a DOI up to confirm it is
registered. It holds no credentials, keeps no database, and stores nothing, so there is
nothing to back up and nothing to leak. Staff upload the generated files themselves.

All settings come from environment variables, so the same code runs on Fly and under
NSSM on the Clearinghouse's Windows server. No view builds an absolute URL, which keeps
the app indifferent to the ARR Host-header quirk described in the Pinakes notes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from flask import (Flask, Response, redirect, render_template, request, session,
                   url_for)

import crossref_api
import xmlbuild as xb
from refs import split_references, doi_in_text

BASE = Path(__file__).parent
PREFIX = os.environ.get("WACDOI_PREFIX", "10.37514")
REGISTRANT = os.environ.get("WACDOI_REGISTRANT", "WAC Clearinghouse")
JOURNALS = json.loads((BASE / "data" / "journals.json").read_text(encoding="utf-8"))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ISSN_RE = re.compile(r"^\d{4}-\d{3}[\dX]$")
URL_RE = re.compile(r"^https?://\S+$", re.I)
SLUG_RE = re.compile(r"^[a-z0-9]+$")
MIN_ROWS = {"issue": 6, "article": 1}

# One shared passphrase gates everything except /health and /login. Unset means open,
# which is right for local development and wrong for a server; the install script sets it.
ACCESS_CODE = os.environ.get("WACDOI_ACCESS_CODE", "")
LOGIN_WINDOW, LOGIN_MAX_FAILS = 15 * 60, 10

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
# The session key is derived from the passphrase, so changing the passphrase also signs
# everyone out. No second secret to keep in step.
app.secret_key = hashlib.sha256(("wacdoi-session:" + ACCESS_CODE).encode()).digest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("WACDOI_SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
_fails: dict[str, deque] = defaultdict(deque)


@app.after_request
def headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def _client_key() -> str:
    # Behind ARR every request arrives from 127.0.0.1. ARR appends the client address to
    # X-Forwarded-For, so the LAST entry is the one the proxy vouches for. The first can be
    # forged by the client, which would let it dodge the lockout by changing it each try.
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[-1].strip() or request.remote_addr or "?"


@app.before_request
def require_login():
    if not ACCESS_CODE or request.endpoint in ("health", "login", "static"):
        return None
    if session.get("ok"):
        return None
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not ACCESS_CODE:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        key, now = _client_key(), time.time()
        recent = _fails[key]
        while recent and now - recent[0] > LOGIN_WINDOW:
            recent.popleft()
        if len(recent) >= LOGIN_MAX_FAILS:
            return render_template("login.html", error="Too many attempts. Wait fifteen "
                                   "minutes and try again."), 429
        if hmac.compare_digest(request.form.get("code", "").encode(), ACCESS_CODE.encode()):
            session.clear()
            session.permanent = True
            session["ok"] = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                            else url_for("index"))
        recent.append(now)
        error = "That passphrase is not right."
    return render_template("login.html", error=error), (401 if error else 200)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.context_processor
def inject():
    return {"gated": bool(ACCESS_CODE), "prefix": PREFIX, "journals": JOURNALS, "default_registrant": REGISTRANT,
            "schemas_ok": xb.schemas_available()}


def issn_ok(s: str) -> bool:
    if not ISSN_RE.match(s):
        return False
    d = s.replace("-", "")
    total = sum(int(c) * w for c, w in zip(d[:7], range(8, 1, -1)))
    check = (11 - total % 11) % 11
    return d[7] == ("X" if check == 10 else str(check))


def slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


# -- pages -----------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return {"ok": True, "schemas": xb.schemas_available(), "prefix": PREFIX}


# -- step one --------------------------------------------------------------

def _blank_row():
    return {"title": "", "subtitle": "", "authors": "", "first": "", "last": "",
            "doi": "", "url": ""}


def _form_values(form):
    return {k: form.get(k, "").strip() for k in (
        "dep_name", "dep_email", "registrant", "journal", "j_slug", "j_title", "j_abbrev",
        "j_issn_print", "j_issn_online", "mode", "year", "month", "day", "volume",
        "issue", "start_no")}


def _rows_from(form):
    cols = ["title", "subtitle", "authors", "first", "last", "doi", "url"]
    lists = {c: form.getlist(f"a_{c}") for c in cols}
    n = max((len(v) for v in lists.values()), default=0)
    return [{c: (lists[c][i].strip() if i < len(lists[c]) else "") for c in cols}
            for i in range(n)]


def _parse_authors(text: str, errors: list[str], where: str) -> list[xb.Author]:
    out = []
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()]:
        name, _, orcid = (p.strip() for p in line.partition("|"))
        orcid = re.sub(r"^https?://orcid\.org/", "", orcid, flags=re.I)
        if "," not in name:
            errors.append(f"{where}: write each author as Surname, Given name "
                          f"(got “{line}”). One author per line.")
            continue
        surname, _, given = (p.strip() for p in name.partition(","))
        if not surname:
            errors.append(f"{where}: missing surname in “{line}”.")
            continue
        if orcid and not xb.orcid_ok(orcid):
            errors.append(f"{where}: ORCID “{orcid}” for {surname} fails its "
                          "checksum. Check it against orcid.org.")
            orcid = ""
        out.append(xb.Author(surname=surname, given=given, orcid=orcid))
    return out


def _preset(slug: str):
    return next((j for j in JOURNALS if j["slug"] == slug), None)


def parse_metadata(form):
    """Return (values, rows, errors, warnings, built) where built is None on any error."""
    v, rows = _form_values(form), _rows_from(form)
    errors: list[str] = []
    warnings: list[str] = []
    mode = v["mode"] if v["mode"] in MIN_ROWS else "issue"
    v["mode"] = mode

    if not v["dep_name"]:
        errors.append("Depositor name is required.")
    if not EMAIL_RE.match(v["dep_email"]):
        errors.append("A valid depositor email is required. Crossref sends the success "
                      "or error report there, so it must be an inbox someone reads.")
    if not v["registrant"]:
        errors.append("Registrant is required.")

    preset = _preset(v["journal"])
    slug = v["j_slug"].lower() if v["j_slug"] else (preset["slug"] if preset else "")
    title = preset["title"] if preset else v["j_title"]
    abbrev = preset["abbrev"] if preset else v["j_abbrev"]
    issn_p = preset["issn_print"] if preset else v["j_issn_print"]
    issn_o = preset["issn_online"] if preset else v["j_issn_online"]
    if not title:
        errors.append("Choose a journal or enter its full title.")
    for label, s in (("Print ISSN", issn_p), ("Online ISSN", issn_o)):
        if s and not issn_ok(s):
            errors.append(f"{label} “{s}” is not a valid ISSN (format 1234-5678, "
                          "with a correct check digit).")
    if not (issn_p or issn_o):
        warnings.append("No ISSN. Crossref uses the ISSN to attach articles to the right "
                        "journal record, so a deposit without one can create a duplicate title.")
    if slug and not SLUG_RE.match(slug):
        errors.append("The journal code in DOIs (the part before “-j.”) may "
                      "contain only lowercase letters and digits.")

    year = v["year"]
    if not re.fullmatch(r"\d{4}", year) or not 1900 <= int(year) <= 2100:
        errors.append("Publication year must be four digits.")
    if v["month"] and not (v["month"].isdigit() and 1 <= int(v["month"]) <= 12):
        errors.append("Month must be a number from 1 to 12.")
    if v["day"] and not (v["day"].isdigit() and 1 <= int(v["day"]) <= 31):
        errors.append("Day must be a number from 1 to 31.")
    if v["day"] and not v["month"]:
        errors.append("A day needs a month.")
    for label, key in (("Volume", "volume"), ("Issue", "issue")):
        if len(v[key]) > 32:
            errors.append(f"{label} is longer than Crossref's 32-character limit.")
    if not v["volume"] and not v["issue"]:
        warnings.append("No volume or issue given. Most journals need at least one.")

    try:
        start = int(v["start_no"] or 1)
    except ValueError:
        start = 1
        errors.append("Starting article number must be a whole number.")

    articles: list[xb.Article] = []
    seen: set[str] = set()
    live = [r for r in rows if any(r.values())]
    if not live:
        errors.append("Add at least one article.")
    for i, r in enumerate(live):
        where = f"Article {i + 1}"
        if not r["title"]:
            errors.append(f"{where}: title is required.")
        if not URL_RE.match(r["url"]):
            errors.append(f"{where}: the URL where the article lives is required "
                          "(starting http:// or https://).")
        doi = xb.normalize_doi(r["doi"])
        if not doi:
            if slug and year:
                parts = [year, v["volume"], v["issue"], f"{start + i:02d}"]
                doi = f"{PREFIX}/{slug}-j." + ".".join(p for p in parts if p)
                r["doi"] = doi
                warnings.append(f"{where}: no DOI entered, so {doi} was generated from the "
                                "Clearinghouse pattern. Confirm it matches this journal's "
                                "earlier DOIs.")
            else:
                errors.append(f"{where}: enter a DOI, or give the journal code and year so "
                              "one can be generated.")
        if doi:
            if not doi.lower().startswith(PREFIX.lower() + "/"):
                errors.append(f"{where}: DOI must start with {PREFIX}/ (this tool only "
                              "mints under the Clearinghouse prefix).")
            elif not xb.DOI_RE.match(doi):
                errors.append(f"{where}: DOI “{doi}” contains characters Crossref "
                              "no longer allows in new DOIs. Suffixes may use letters, "
                              "digits and - . _ ; ( ) /")
            if doi.lower() in seen:
                errors.append(f"{where}: DOI {doi} appears twice in this batch.")
            seen.add(doi.lower())
        authors = _parse_authors(r["authors"], errors, where)
        if not authors:
            warnings.append(f"{where}: no authors. Crossref strongly recommends them.")
        if r["last"] and not r["first"]:
            errors.append(f"{where}: a last page needs a first page.")
        articles.append(xb.Article(
            title=r["title"], subtitle=r["subtitle"], doi=doi, url=r["url"], authors=authors,
            first_page=r["first"], last_page=r["last"]))

    built = None
    if not errors:
        built = (xb.Depositor(v["dep_name"], v["dep_email"], v["registrant"]),
                 xb.Journal(title, abbrev, issn_p, issn_o),
                 xb.Issue(year, v["volume"], v["issue"], v["month"], v["day"]),
                 articles, slug)
    return v, rows, errors, warnings, built


def _existing(dois: list[str]) -> list[crossref_api.Lookup]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(crossref_api.lookup, dois))


def _rows_for_render(rows, mode):
    rows = list(rows)
    while len(rows) < MIN_ROWS[mode]:
        rows.append(_blank_row())
    return rows


@app.route("/metadata", methods=["GET", "POST"])
def metadata():
    if request.method == "GET":
        mode = "article" if request.args.get("mode") == "article" else "issue"
        v = {"mode": mode, "registrant": REGISTRANT, "start_no": "1"}
        return render_template("metadata.html", v=v, rows=_rows_for_render([], mode),
                               errors=[], warnings=[])

    v, rows, errors, warnings, built = parse_metadata(request.form)
    action = request.form.get("action", "build")
    if action == "add_row" or errors or built is None:
        if action == "add_row":
            rows.append(_blank_row())
            errors = []
        return render_template("metadata.html", v=v, rows=_rows_for_render(rows, v["mode"]),
                               errors=errors, warnings=warnings if action != "add_row" else [])

    dep, journal, issue, articles, slug = built
    # Anything already registered under these DOIs will be overwritten by this deposit.
    for art, found in zip(articles, _existing([a.doi for a in articles])):
        if found.status == crossref_api.FOUND:
            warnings.append(
                f"{art.doi} is ALREADY REGISTERED (“{found.title}”). Uploading this "
                "file will overwrite that record. Continue only if that is the intent.")
        elif found.status == crossref_api.UNREACHABLE:
            warnings.append("Could not reach Crossref to check whether these DOIs already "
                            "exist. Check them yourself before uploading.")
            break

    stamp = xb.now_stamp()
    batch_id = f"wacc-{slug or 'meta'}-{stamp}"
    xml = xb.build_metadata(dep, journal, issue, articles, batch_id, stamp)
    problems = xb.validate(xml, "metadata") if xb.schemas_available() else \
        ["Schema files are missing on this server (run tools/fetch_schemas.py)."]
    fname = "-".join(p for p in ["wacc", "metadata", slug, issue.year, issue.volume,
                                 issue.issue, stamp] if p) + ".xml"
    return render_template("metadata_result.html", v=v, journal=journal, issue=issue,
                           articles=articles, warnings=warnings, problems=problems,
                           xml=xml.decode("utf-8"), filename=slugify(fname[:-4]) + ".xml",
                           batch_id=batch_id)


def _download(kind: str):
    xml = request.form.get("xml", "").encode("utf-8")
    name = re.sub(r"[^A-Za-z0-9._-]", "-", request.form.get("filename", "deposit.xml"))
    if not xb.schemas_available():
        return Response("Schema files are missing on this server, so the file cannot be "
                        "validated and is not served.", status=503)
    problems = xb.validate(xml, kind)
    if problems:
        return Response("The file does not pass Crossref's schema:\n" + "\n".join(problems),
                        status=422, mimetype="text/plain")
    return Response(xml, mimetype="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/metadata/download")
def metadata_download():
    return _download("metadata")


# -- step two --------------------------------------------------------------

def _refs_gate(form):
    """Shared checks for both stages of step two. Returns (values, record, errors)."""
    v = {k: form.get(k, "").strip() for k in ("dep_name", "dep_email", "doi")}
    v["override"] = form.get("override") == "1"
    errors: list[str] = []
    if not v["dep_name"]:
        errors.append("Depositor name is required.")
    if not EMAIL_RE.match(v["dep_email"]):
        errors.append("A valid depositor email is required.")
    doi = xb.normalize_doi(v["doi"])
    v["doi"] = doi
    record = None
    if not doi:
        errors.append("The article's DOI is required. Reference lists attach to a DOI that "
                      "already exists, so mint it with step one first.")
    elif not doi.lower().startswith(PREFIX.lower() + "/"):
        errors.append(f"DOI must start with {PREFIX}/.")
    elif not xb.DOI_RE.match(doi):
        errors.append("That does not look like a valid DOI.")
    else:
        record = crossref_api.lookup(doi)
        if record.status == crossref_api.MISSING and not v["override"]:
            errors.append(
                f"Crossref has no record of {doi}. Either it has a typo, or the step-one "
                "deposit has not been processed yet (this can take several minutes after "
                "upload; check the confirmation email). Do this step after the DOI exists.")
        elif record.status == crossref_api.UNREACHABLE and not v["override"]:
            errors.append("Could not reach Crossref to confirm the DOI. Try again, or tick "
                          "the box below to proceed if you have confirmed it is registered.")
    return v, record, errors


@app.route("/references", methods=["GET", "POST"])
def references():
    if request.method == "GET":
        return render_template("references.html", v={}, text="", errors=[], record=None)

    text = request.form.get("text", "")
    v, record, errors = _refs_gate(request.form)
    entries = split_references(text)
    if not entries:
        errors.append("Paste the Works Cited or References list. Just that list, with no "
                      "article text.")
    if errors:
        return render_template("references.html", v=v, text=text, errors=errors,
                               record=record,
                               show_override=bool(record and record.status != crossref_api.FOUND))
    return render_template("references_review.html", v=v, record=record, entries=entries)


@app.post("/references/build")
def references_build():
    v, record, errors = _refs_gate(request.form)
    items = []
    for t in request.form.getlist("ref"):
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            items.append(xb.Reference(t, doi_in_text(t)))
    if not items:
        errors.append("Every entry was removed. Nothing to deposit.")
    if errors:
        return render_template("references.html", v=v, text="\n\n".join(i.text for i in items),
                               errors=errors, record=record)

    stamp = xb.now_stamp()
    batch_id = f"wacc-refs-{stamp}"
    xml = xb.build_references(v["dep_name"], v["dep_email"], [(v["doi"], items)], batch_id)
    problems = xb.validate(xml, "references") if xb.schemas_available() else \
        ["Schema files are missing on this server (run tools/fetch_schemas.py)."]
    suffix = slugify(v["doi"].split("/", 1)[1]) if "/" in v["doi"] else "doi"
    return render_template("references_result.html", v=v, record=record, items=items,
                           problems=problems, xml=xml.decode("utf-8"),
                           filename=f"wacc-references-{suffix}-{stamp}.xml", batch_id=batch_id)


@app.post("/references/download")
def references_download():
    return _download("references")


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))
