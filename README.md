# WAC Clearinghouse DOI deposit tool

Two steps, always in this order. Each produces an XML file that staff upload at the Crossref
admin site. The tool deposits nothing, holds no Crossref credentials, and stores nothing.

1. **`/metadata`: mint DOIs.** Whole issue or single article. Output is a metadata deposit
   (schema 5.4.0) registering DOIs under `10.37514`.
2. **`/references`: add references.** Paste a Works Cited from a Word or text file. The
   article's DOI is required, and the tool asks Crossref's public API whether that DOI exists
   before it builds anything. Output is a resources-only deposit (`doi_resources` 4.3.6) that
   binds the citation list to the existing DOI.

Every file is validated against Crossref's own XSDs before it can be downloaded. The download
endpoint re-validates, so a hand-edited file cannot be served either.

## Run it

```bash
pip install -r requirements-dev.txt
python tools/fetch_schemas.py      # once; saves Crossref's XSDs into schemas/ (~1.5 MB)
python -m pytest tests -q          # 31 tests, none touch the network
python app.py                      # http://localhost:5000
```

## Decisions worth knowing

- **References go in as `<unstructured_citation>`.** Citation style is irrelevant: Crossref
  matches the text to DOIs itself. The tool only splits the pasted block into entries (blank
  lines, hanging indents, or one per line) and shows them for review before generating. A DOI
  found inside an entry is also written as `<doi>`.
- **A reference deposit replaces the DOI's whole list.** The review screen warns when Crossref
  already shows references for that DOI.
- **A DOI that is already registered is flagged** on step one, because uploading the file
  overwrites that record.
- **Generated DOIs follow the Clearinghouse grammar** `10.37514/<code>-j.<year>.<vol>.<issue>.<nn>`,
  and are labelled as generated. Issue is copied as typed because the existing records differ
  (`wac-j.1997.8.01.14` pads, `jbw-j.2009.28.2.06` doesn't).
- `data/journals.json` holds titles, codes and ISSNs read from Crossref's records under the
  prefix. Crossref lists every ISSN there as electronic. **Staff should confirm print/online
  and add abbreviated titles.** Journals not in the list can be typed in on the form.
- **Three kinds of work.** `/mint` asks first: journal article(s) (`/metadata`), edited
  collection or monograph (`/book`). Books use Crossref's `<book>` element in the same 5.4.0
  schema: an edited collection gets the volume DOI plus a `content_item` per chapter, with
  editors on the volume and authors on each chapter; a monograph has its own DOI and optional
  chapters. Book DOIs are never generated (enter them as the series numbers them); blank
  chapter DOIs are suggested as `<book DOI>.NN` and flagged for confirmation. Optional
  series (needs an ISSN) and ISBNs (checksummed) are supported. Step 2 works unchanged on
  any of these DOIs.
- Nothing builds an absolute URL, so a reverse proxy's Host-header handling cannot affect it.

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `WACDOI_PREFIX` | `10.37514` | the only prefix the tool will mint or attach references under |
| `WACDOI_REGISTRANT` | `WAC Clearinghouse` | pre-fills the registrant field |
| `CROSSREF_MAILTO` | *(empty)* | sent to Crossref's API so requests go to the polite pool; set it |
| `WACDOI_API_TIMEOUT` | `8` | seconds to wait on Crossref's API |
| `WACDOI_ACCESS_CODE` | *(empty = open)* | shared passphrase for the whole site |
| `WACDOI_SECURE_COOKIES` | *(off)* | set to `1` when served over HTTPS |

## Deploy: Fly.io (interim)

```bash
fly launch --no-deploy --copy-config
fly deploy
```

## Deploy: Windows server

Waitress behind IIS with Application Request Routing, run as a service by NSSM, installed from
a clone of this repository. `deploy/windows/Install-WaccDoi.ps1 -Plan` runs read-only checks
first; run without `-Plan` to install. `Update-WaccDoi.ps1` pulls, reinstalls requirements,
restarts and health-checks. Both target Windows PowerShell 5.1.

## Access

Set `WACDOI_ACCESS_CODE` and every page except `/health` requires that shared passphrase.
Sessions last twelve hours, ten wrong guesses lock an address out for fifteen minutes, and
changing the passphrase signs everyone out. With it unset the app is open, which is only
right for local development. The installer always sets it.

## Not built yet

- **Depositing straight to Crossref.** Possible (HTTPS POST to `doi.crossref.org/servlet/deposit`
  with `doMDUpload` / `doDOICitUpload`), but it means holding credentials for a shared prefix.
  If added, do the test system (`test.crossref.org`) first and keep the password out of storage.
- Several articles' references in one resources deposit. The generator already supports it
  (`build_references` takes a list of DOIs); the form takes one article at a time.
- Issue-level or journal-level DOIs, book parts/sections, and reference or other book types.
