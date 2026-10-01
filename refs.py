# -*- coding: utf-8 -*-
"""Split a pasted Works Cited / References list into individual entries.

Crossref takes each reference as an unstructured string and does the matching on its
own side, so style (APA, MLA, Chicago) doesn't matter and nothing here parses authors
or titles. The only hard problem is finding where one entry ends and the next begins
in text pasted out of Word. The splitter is a best guess; the review screen exists
because it will sometimes be wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from xmlbuild import DOI_RE

HEADING = re.compile(
    r"^\W*(works\s+cited|references?|bibliography|sources|works\s+consulted)\W*$", re.I)
LEADING_NUMBER = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s+")
# Reference starts: "Surname," / "Surname, A." / a repeated-author dash / an
# organisation name followed by a period. Deliberately loose; used only to decide
# whether a line that follows an unfinished line is a continuation.
ENTRY_START = re.compile(r"^(?:[-—–_]{2,}\.?|[A-ZÀ-Þ][\w'’.\-À-ɏ]+,)")
SENTENCE_END = re.compile(r"[.?!)\"”’'\]]\s*$")
DOI_IN_TEXT = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
DASH_AUTHOR = re.compile(r"^(?:[-—–_]{2,})")


@dataclass
class Entry:
    text: str
    doi: str = ""
    warnings: list[str] = field(default_factory=list)


def _clean(s: str) -> str:
    s = s.replace(" ", " ").replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


def _drop_heading(lines: list[str]) -> list[str]:
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and HEADING.match(lines[i].strip()):
        return lines[i + 1:]
    return lines


def _chunks(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    lines = _drop_heading(text.split("\n"))
    body = "\n".join(lines)

    # 1. Blank lines between entries: the unambiguous case.
    blocks = [b for b in re.split(r"\n\s*\n", body) if b.strip()]
    if len(blocks) > 1:
        return blocks

    lines = [ln for ln in body.split("\n") if ln.strip()]
    if len(lines) <= 1:
        return lines

    # 2. Hanging indent: a flush-left line opens an entry, indented lines continue it.
    indented = [ln[:1].isspace() for ln in lines]
    if any(indented) and not all(indented):
        out: list[str] = []
        for ln, ind in zip(lines, indented):
            if ind and out:
                out[-1] += " " + ln.strip()
            else:
                out.append(ln.strip())
        return out

    # 3. One line per entry, unless a line looks like the middle of the previous one.
    out = []
    for ln in lines:
        s = ln.strip()
        if out and not SENTENCE_END.search(out[-1]) and not ENTRY_START.match(s):
            out[-1] += " " + s
        elif out and s[:1].islower():
            out[-1] += " " + s
        else:
            out.append(s)
    return out


def doi_in_text(text: str) -> str:
    m = DOI_IN_TEXT.search(text)
    if not m:
        return ""
    doi = m.group(0).rstrip(".,;:)]}'”’")
    return doi if DOI_RE.match(doi) else ""


def split_references(text: str, max_len: int = 2000) -> list[Entry]:
    entries: list[Entry] = []
    seen: set[str] = set()
    for chunk in _chunks(text or ""):
        s = _clean(LEADING_NUMBER.sub("", chunk, count=1))
        if not s:
            continue
        e = Entry(text=s, doi=doi_in_text(s))
        if s.lower() in seen:
            e.warnings.append("Duplicate of an earlier entry.")
        seen.add(s.lower())
        if DASH_AUTHOR.match(s):
            e.warnings.append(
                "Starts with a dash in place of the author. Crossref matches better with the "
                "name written out; replace the dash with the author from the entry above.")
        if len(s) < 25:
            e.warnings.append("Very short for a reference; may be a fragment.")
        if len(s) > max_len:
            e.warnings.append(
                "Very long; two entries may have run together. Split it or trim it.")
        entries.append(e)
    return entries
