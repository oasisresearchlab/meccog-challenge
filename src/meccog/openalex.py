"""OpenAlex: a second, independent citation graph and search index, plus open-access locations with
their version (published / accepted / submitted). Keyless use has a small daily credit budget; set
OPENALEX_API_KEY for more. Every call is cheap except search (10 credits a page).
"""

from __future__ import annotations

import os
import time

import requests

from .search import UA

API = "https://api.openalex.org"
SELECT = ("id,ids,doi,title,publication_year,type,authorships,primary_location,abstract_inverted_index,"
          "open_access,best_oa_location,locations,referenced_works,is_retracted")
VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}


class NeedsKey(RuntimeError):
    """OpenAlex refused a keyless request (deep paging, or the daily credit budget)."""


def _get(path: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    for attempt in range(4):
        r = requests.get(f"{API}{path}", params=params, headers=UA, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return {}
        if r.status_code in (401, 403):
            raise NeedsKey(f"OpenAlex {r.status_code} on {path}: set OPENALEX_API_KEY")
        time.sleep(2 ** attempt + (5 if r.status_code == 429 else 0))
    r.raise_for_status()
    return {}


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = {i: w for w, idx in inv.items() for i in idx}
    return " ".join(pos[i] for i in sorted(pos))


def to_record(w: dict) -> dict:
    ids = w.get("ids") or {}
    pmid = (ids.get("pmid") or "").rsplit("/", 1)[-1] or None
    pmcid = (ids.get("pmcid") or "").rsplit("/", 1)[-1] or None
    if pmcid and not pmcid.startswith("PMC"):
        pmcid = "PMC" + pmcid
    src = ((w.get("primary_location") or {}).get("source") or {})
    return {
        "epmc_id": None, "epmc_source": None, "openalex_id": w["id"].rsplit("/", 1)[-1],
        "pmid": pmid, "pmcid": pmcid,
        "doi": (w.get("doi") or "").removeprefix("https://doi.org/") or None,
        "title": (w.get("title") or "").strip(),
        "authors": ", ".join(a["author"]["display_name"] for a in (w.get("authorships") or [])[:12]),
        "journal": src.get("display_name") or "", "year": str(w.get("publication_year") or ""),
        "abstract": _abstract(w.get("abstract_inverted_index")),
        "is_preprint": w.get("type") == "preprint", "is_review": w.get("type") == "review",
        "open_access": bool((w.get("open_access") or {}).get("is_oa")),
        "pubtypes": [w.get("type") or ""], "retracted": bool(w.get("is_retracted")),
    }


def work(pmid: str | None = None, doi: str | None = None) -> dict | None:
    key = f"pmid:{pmid}" if pmid else f"doi:{doi}"
    w = _get(f"/works/{key}", {"select": SELECT})
    return w or None


def works_by_ids(oa_ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(oa_ids), 50):
        chunk = [x.rsplit("/", 1)[-1] for x in oa_ids[i:i + 50]]
        out += _get("/works", {"filter": "openalex:" + "|".join(chunk), "per-page": 50,
                               "select": SELECT}).get("results", [])
    return out


def cited_by(oa_id: str, limit: int = 1000) -> tuple[list[dict], bool]:
    """(works, complete). Keyless access stops after the first page of 200."""
    out, cursor = [], "*"
    while cursor and len(out) < limit:
        try:
            d = _get("/works", {"filter": f"cites:{oa_id}", "per-page": 200, "cursor": cursor, "select": SELECT})
        except NeedsKey:
            return out, False
        out += d.get("results", [])
        cursor = (d.get("meta") or {}).get("next_cursor")
        if not d.get("results"):
            break
    return out, True


def search(query: str, limit: int = 200) -> list[dict]:
    d = _get("/works", {"search": query, "per-page": min(200, limit), "select": SELECT})
    return d.get("results", [])


def oa_pdfs(w: dict) -> list[dict]:
    """Open-access PDF locations, the published version first."""
    locs = [l for l in (w.get("locations") or []) if l.get("is_oa") and l.get("pdf_url")]
    locs.sort(key=lambda l: VERSION_RANK.get(l.get("version") or "", 3))
    return [{"url": l["pdf_url"], "version": l.get("version") or "unknown",
             "host": ((l.get("source") or {}).get("display_name") or "")} for l in locs]
