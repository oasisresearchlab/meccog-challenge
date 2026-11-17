"""The access queue: papers that screened as relevant but have no open published or accepted version we can
fetch programmatically. People with institutional access claim rows, save PDFs into pdfs/ under the
`save_as` name, and the next `meccog extract` picks them up as the `local` route.

Built from screen.jsonl across every hypothesis run, so one paper relevant to three hypotheses is one row.
Access is checked without downloading: a PMC S3 listing and an OpenAlex lookup per paper.
Writes runs/access_queue.csv (paste into a shared sheet) and runs/access_queue.md.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import requests

from . import openalex
from .fulltext import S3, local_names
from .search import UA

COLUMNS = ["priority", "relevance", "hypotheses", "why_relevant", "title", "first_author", "year", "journal",
           "doi_url", "pmid", "save_as", "access_now", "claimed_by", "done"]


def pmc_has_pdf(pmcid: str | None) -> bool:
    if not pmcid:
        return False
    try:
        r = requests.get(S3 + "/", params={"list-type": "2", "prefix": f"{pmcid}.", "max-keys": "200"},
                         headers=UA, timeout=30)
    except requests.RequestException:
        return False
    return bool(re.search(rf"<Key>{pmcid}\.\d+/{pmcid}\.\d+\.pdf</Key>", r.text))


def access_now(rec: dict) -> str:
    """'open' (published/accepted PDF reachable), 'preprint only', or 'closed'."""
    if pmc_has_pdf(rec.get("pmcid")):
        return "open"
    try:
        w = openalex.work(pmid=rec["pmid"]) if rec.get("pmid") else openalex.work(doi=rec.get("doi"))
    except openalex.NeedsKey:
        return "unknown (OpenAlex budget)"
    versions = {l["version"] for l in openalex.oa_pdfs(w)} if w else set()
    if versions & {"publishedVersion", "acceptedVersion"}:
        return "open"
    return "preprint only" if versions else "closed"


def build(runs: Path, pdf_dir: Path, min_relevance: float = 0.6) -> Path:
    rows: dict[str, dict] = {}
    for hdir in sorted(p for p in runs.glob("H*") if (p / "screen.jsonl").exists()):
        cands = {(c["pmid"] or c["doi"] or c.get("epmc_id") or c.get("openalex_id")): c
                 for c in map(json.loads, (hdir / "candidates.jsonl").read_text().splitlines())}
        for v in map(json.loads, (hdir / "screen.jsonl").read_text().splitlines()):
            if not (v["include"] and v["is_primary_research"] and v["relevance"] >= min_relevance):
                continue
            c = cands.get(v["key"])
            if not c:
                continue
            k = v["key"]
            row = rows.setdefault(k, {"rec": c, "rel": {}, "why": {}})
            row["rel"][hdir.name] = v["relevance"]
            row["why"][hdir.name] = v["reason"]

    out = []
    for k, row in rows.items():
        c = row["rec"]
        names = local_names(c)
        if any((pdf_dir / f"{n}.pdf").exists() for n in names):
            continue
        status = access_now(c)
        if status == "open":
            continue
        best = max(row["rel"].values())
        hyps = sorted(row["rel"], key=lambda h: -row["rel"][h])
        out.append({
            "priority": round(best + 0.05 * (len(hyps) - 1) + (0.0 if status == "closed" else -0.05), 3),
            "relevance": best, "hypotheses": " ".join(hyps),
            "why_relevant": row["why"][hyps[0]], "title": c["title"],
            "first_author": (c.get("authors") or "").split(",")[0], "year": c.get("year"),
            "journal": c.get("journal"),
            "doi_url": f"https://doi.org/{c['doi']}" if c.get("doi") else "",
            "pmid": c.get("pmid") or "", "save_as": f"{names[0]}.pdf" if names else "",
            "access_now": status, "claimed_by": "", "done": ""})
    out.sort(key=lambda r: -r["priority"])

    csv_path = runs / "access_queue.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out)
    md = ["# Access queue", "",
          "Relevant papers with no open published or accepted version. Claim one, download it through your "
          "library, and save it into `pdfs/` under the `save_as` name (supplements as `<PMID>_supp1.pdf`, ...). "
          "`preprint only` means we can extract from the preprint, but its panels and numbers may differ from "
          "the published paper.", "",
          "| # | rel | hyp | access | paper | save as |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(out, 1):
        link = f"[{r['title'][:90]}]({r['doi_url']})" if r["doi_url"] else r["title"][:90]
        md.append(f"| {i} | {r['relevance']:.2f} | {r['hypotheses']} | {r['access_now']} | "
                  f"{r['first_author']} {r['year']}, {link} | `{r['save_as']}` |")
    (runs / "access_queue.md").write_text("\n".join(md) + "\n")
    print(f"access queue: {len(out)} papers ({sum(r['access_now'] == 'closed' for r in out)} closed, "
          f"{sum(r['access_now'] == 'preprint only' for r in out)} preprint only) -> {csv_path}")
    return csv_path
