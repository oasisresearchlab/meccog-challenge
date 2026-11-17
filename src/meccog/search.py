"""Step 1: candidate papers for a hypothesis, from Europe PMC (which indexes all of PubMed plus preprints).

Three routes, all logged to the run so the method README can report them, including the ones that
returned nothing:
  - seed expansion: the starting reference, the papers it cites, and the papers citing it
  - model-written queries: Claude turns the hypothesis into Europe PMC query strings
  - extra queries: any the user passes by hand (--query)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from . import claude

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UA = {"User-Agent": "meccog-pipeline/0.1 (open-source literature extraction)"}

QUERY_SYSTEM = """You write literature search queries for Europe PMC (Lucene syntax: AND, OR, NOT,
quoted phrases, parentheses; fields like TITLE:, ABSTRACT:, PUB_TYPE: are allowed).

You get one mechanism hypothesis about how APOE4 (vs APOE3) alters endosome/lysosome biology.
Write 6-12 queries that together find the primary experimental papers bearing on it, for or against.
Cover: the exact mechanism wording; synonyms for each variable (e.g. apoE4, ApoE4, APOE ε4, E4;
endosome, early/late endosome, endolysosome, lysosome, recycling endosome, Rab5/Rab7/Rab11); the
specific molecules named; the cell types (astrocytes, neurons, microglia, iPSC-derived cells, APOE
targeted-replacement mice). Include at least one query aimed at null or contradicting results.
Keep each query precise enough to return under ~300 hits. Do not add date limits."""

QUERY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["queries"],
    "properties": {"queries": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["query", "aim"],
        "properties": {"query": {"type": "string"}, "aim": {"type": "string"}}}}},
}


def _get(url: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        r = requests.get(url, params=params, headers=UA, timeout=60)
        if r.status_code == 200:
            return r.json()
        time.sleep(2 ** attempt)
    r.raise_for_status()
    return {}


def _record(hit: dict) -> dict:
    """Normalize a Europe PMC 'core' result to the fields the pipeline uses."""
    pubtypes = [t.lower() for t in (hit.get("pubTypeList") or {}).get("pubType", [])]
    return {
        "epmc_id": hit.get("id"), "epmc_source": hit.get("source"),
        "pmid": hit.get("pmid"), "pmcid": hit.get("pmcid"), "doi": hit.get("doi"),
        "title": (hit.get("title") or "").strip(),
        "authors": hit.get("authorString", ""),
        "journal": ((hit.get("journalInfo") or {}).get("journal") or {}).get("title")
        or hit.get("journalTitle") or (hit.get("bookOrReportDetails") or {}).get("publisher", ""),
        "year": hit.get("pubYear"),
        "abstract": hit.get("abstractText", "") or "",
        "is_preprint": hit.get("source") == "PPR" or "preprint" in pubtypes,
        "is_review": any(t in pubtypes for t in ("review", "review-article", "systematic review")),
        "open_access": hit.get("isOpenAccess") == "Y",
        "pubtypes": pubtypes,
    }


def epmc_search(query: str, limit: int = 300) -> list[dict]:
    out, cursor = [], "*"
    while len(out) < limit:
        data = _get(f"{EPMC}/search", {"query": query, "format": "json", "resultType": "core",
                                       "pageSize": min(100, limit - len(out)), "cursorMark": cursor})
        hits = (data.get("resultList") or {}).get("result", [])
        out += [_record(h) for h in hits]
        nxt = data.get("nextCursorMark")
        if not hits or not nxt or nxt == cursor:
            break
        cursor = nxt
    return out


def by_pmid(pmids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(pmids), 50):
        chunk = pmids[i:i + 50]
        q = "(" + " OR ".join(f"EXT_ID:{p}" for p in chunk) + ") AND SRC:MED"
        out += epmc_search(q, limit=len(chunk))
    return out


def links(pmid: str, kind: str) -> list[str]:
    """kind = 'citations' (papers citing pmid) or 'references' (papers pmid cites). PubMed IDs only."""
    key = "citationList" if kind == "citations" else "referenceList"
    item = "citation" if kind == "citations" else "reference"
    out, page = [], 1
    while True:
        data = _get(f"{EPMC}/MED/{pmid}/{kind}", {"format": "json", "pageSize": 1000, "page": page})
        rows = (data.get(key) or {}).get(item, [])
        out += [r["id"] for r in rows if r.get("source") == "MED" and r.get("id")]
        if len(rows) < 1000:
            return out
        page += 1


def model_queries(hyp: dict) -> list[dict]:
    data, _ = claude.json_call(QUERY_SYSTEM, [{"type": "text", "text": hyp["statement"]}],
                               QUERY_SCHEMA, model=claude.SCREEN_MODEL, effort="medium", max_tokens=16000)
    return data["queries"]


def _openalex_query(q: str) -> str:
    """Europe PMC Lucene -> OpenAlex search: drop field prefixes, keep AND/OR/NOT, quotes, parentheses."""
    return re.sub(r"\b[A-Z_]+:", "", q).replace("*", "")


def run(hyp: dict, run_dir: Path, extra_queries: list[str], per_query: int, use_model: bool,
        use_openalex: bool = True) -> Path:
    from . import openalex

    log: list[dict] = []
    pool: dict[str, dict] = {}
    alias: dict[str, str] = {}          # pmid / doi -> pool key, so one paper found twice merges

    def add(recs: list[dict], route: str):
        for r in recs:
            ids = [x for x in (r.get("pmid"), (r.get("doi") or "").lower(), r.get("epmc_id")) if x]
            if not ids:
                continue
            key = next((alias[i] for i in ids if i in alias), None)
            if key is None:
                key = ids[0]
                pool[key] = {**r, "routes": [route]}
            else:
                cur = pool[key]
                if route not in cur["routes"]:
                    cur["routes"].append(route)
                for k, v in r.items():          # fill gaps (e.g. abstract, pmcid) from the other source
                    if v and not cur.get(k):
                        cur[k] = v
            for i in ids:
                alias[i] = key

    seed = hyp["seed_pmid"]
    add(by_pmid([seed]), "seed")
    for kind in ("references", "citations"):
        recs = by_pmid(links(seed, kind))
        log.append({"source": "europepmc", "route": f"seed {kind}", "query": f"PMID {seed}", "hits": len(recs)})
        add(recs, f"seed-{kind}")

    if use_openalex:
        w = openalex.work(pmid=seed)
        if w:
            refs = openalex.works_by_ids(w.get("referenced_works") or [])
            cites, complete = openalex.cited_by(w["id"].rsplit("/", 1)[-1])
            log.append({"source": "openalex", "route": "seed references", "query": f"PMID {seed}", "hits": len(refs)})
            log.append({"source": "openalex", "route": "seed citations", "query": f"PMID {seed}", "hits": len(cites),
                        **({} if complete else {"note": "truncated: keyless OpenAlex stops at 200; set OPENALEX_API_KEY"})})
            add([openalex.to_record(x) for x in refs], "seed-references")
            add([openalex.to_record(x) for x in cites], "seed-citations")

    queries = [{"query": q, "aim": "user-supplied"} for q in extra_queries]
    if use_model:
        queries += model_queries(hyp)
    for q in queries:
        recs = epmc_search(q["query"], per_query)
        log.append({"source": "europepmc", "route": "query", "query": q["query"], "aim": q["aim"], "hits": len(recs)})
        add(recs, "query")
        if use_openalex:
            oq = _openalex_query(q["query"])
            try:
                recs = [openalex.to_record(x) for x in openalex.search(oq, per_query)]
                log.append({"source": "openalex", "route": "query", "query": oq, "aim": q["aim"], "hits": len(recs)})
            except openalex.NeedsKey as e:
                recs = []
                log.append({"source": "openalex", "route": "query", "query": oq, "aim": q["aim"], "hits": 0,
                            "note": str(e)})
            add(recs, "query-openalex")

    cands = sorted(pool.values(), key=lambda r: (-len(r["routes"]), r["year"] or ""))
    out = run_dir / "candidates.jsonl"
    out.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cands))
    (run_dir / "search_log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
    print(f"{len(cands)} unique candidates")
    return out
