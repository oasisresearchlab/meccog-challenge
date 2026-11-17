"""Step 3: get the full text, trying documented programmatic routes in order and recording which one worked,
so `full_text_unavailable` only ever means every route failed.

  1. local      a PDF you dropped into pdfs/ by hand, named <PMID>.pdf or <DOI with / as _>.pdf.
                This is how institutional access gets in. Supplements go alongside as <PMID>_supp*.pdf.
  2. pmc_s3     the PMC Open Access subset on its public AWS bucket (pmc-oa-opendata), which replaced the
                PMC OA web service in Aug 2026: the article PDF plus any supplementary PDFs, and the
                retraction flag.
  3. openalex   OpenAlex's open-access locations, published version first, then accepted manuscript,
                then preprint. A preprint is used, but marked: its figure panels and numbers can differ
                from the published paper, so the published version also goes on the access queue.
  4. unpaywall  best open-access PDF (optional: needs UNPAYWALL_EMAIL, an address you choose to send)
  5. doi        the doi.org target, only if it answers with a PDF
Otherwise the paper is extracted from its abstract and every statistic is marked accordingly, and it is
listed in needs_pdf.md so a person can fetch it and rerun.

No route impersonates a browser or works around a publisher's access controls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import requests

from .search import UA

S3 = "https://pmc-oa-opendata.s3.amazonaws.com"
MAX_SUPP_BYTES = 20_000_000   # keep the request under the API's 32 MB limit with the main PDF


def local_names(rec: dict) -> list[str]:
    names = []
    if rec.get("pmid"):
        names.append(str(rec["pmid"]))
    if rec.get("doi"):
        names.append(rec["doi"].replace("/", "_"))
    return names


def _is_pdf(p: Path) -> bool:
    try:
        with p.open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def _download(url: str, dest: Path) -> bool:
    try:
        r = requests.get(url, headers={**UA, "Accept": "application/pdf"}, timeout=120)
    except requests.RequestException:
        return False
    if r.status_code == 200 and r.content[:5] == b"%PDF-":
        dest.write_bytes(r.content)
        return True
    return False


def _s3_https(s3url: str) -> str:
    return S3 + "/" + s3url.removeprefix("s3://pmc-oa-opendata/").split("?")[0]


def pmc_s3(pmcid: str, dest_dir: Path) -> dict | None:
    """Latest version of an OA article: {'pdf', 'supplements', 'retracted', 'license'} or None."""
    try:
        r = requests.get(S3 + "/", params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"},
                         headers=UA, timeout=30)
    except requests.RequestException:
        return None
    versions = sorted(int(v) for v in re.findall(rf"<Prefix>{pmcid}\.(\d+)/</Prefix>", r.text))
    if not versions:
        return None
    v = versions[-1]
    try:
        meta = requests.get(f"{S3}/{pmcid}.{v}/{pmcid}.{v}.json", headers=UA, timeout=30).json()
    except (requests.RequestException, ValueError):
        return None
    if not meta.get("pdf_url"):
        return None
    pdf = dest_dir / f"{pmcid}.pdf"
    if not (pdf.exists() and _is_pdf(pdf)) and not _download(_s3_https(meta["pdf_url"]), pdf):
        return None
    supps, total = [], pdf.stat().st_size
    for u in meta.get("media_urls", []):
        name = u.split("/")[-1].split("?")[0]
        if not name.lower().endswith(".pdf"):
            continue
        dest = dest_dir / f"{pmcid}_supp_{name}"
        if not (dest.exists() and _is_pdf(dest)) and not _download(_s3_https(u), dest):
            continue
        if total + dest.stat().st_size > MAX_SUPP_BYTES + pdf.stat().st_size:
            break
        total += dest.stat().st_size
        supps.append(str(dest))
    return {"pdf": str(pdf), "supplements": supps, "retracted": bool(meta.get("is_retracted")),
            "license": meta.get("license_code")}


def fetch(rec: dict, pdf_dir: Path, cache_dir: Path) -> dict:
    tried = ["local"]
    for n in local_names(rec):
        p = pdf_dir / f"{n}.pdf"
        if _is_pdf(p):
            supps = sorted(str(s) for s in pdf_dir.glob(f"{n}_supp*.pdf") if _is_pdf(s))
            return {"route": "local", "pdf": str(p), "supplements": supps, "tried": tried}

    cache_dir.mkdir(parents=True, exist_ok=True)
    if rec.get("pmcid"):
        tried.append("pmc_s3")
        got = pmc_s3(rec["pmcid"], cache_dir)
        if got:
            return {"route": "pmc_s3", **got, "tried": tried}

    stem = (local_names(rec) or [str(rec.get("epmc_id") or rec.get("openalex_id"))])[0]
    dest = cache_dir / f"{stem}.pdf"
    tried.append("openalex")
    try:
        from . import openalex
        w = openalex.work(pmid=rec.get("pmid")) if rec.get("pmid") else openalex.work(doi=rec.get("doi"))
    except Exception:
        w = None
    for loc in (openalex.oa_pdfs(w) if w else []):
        if _download(loc["url"], dest):
            version = loc["version"]
            return {"route": "openalex", "pdf": str(dest), "supplements": [], "version": version,
                    "host": loc["host"], "retracted": bool(w.get("is_retracted")), "tried": tried}
    email = os.environ.get("UNPAYWALL_EMAIL")
    if rec.get("doi") and email:
        tried.append("unpaywall")
        try:
            r = requests.get(f"https://api.unpaywall.org/v2/{rec['doi']}", params={"email": email},
                             headers=UA, timeout=30)
            locs = r.json().get("oa_locations", []) if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            locs = []
        for loc in locs:
            if loc.get("url_for_pdf") and _download(loc["url_for_pdf"], dest):
                return {"route": "unpaywall", "pdf": str(dest), "supplements": [], "tried": tried}

    if rec.get("doi"):
        tried.append("doi")
        if _download(f"https://doi.org/{rec['doi']}", dest):
            return {"route": "doi", "pdf": str(dest), "supplements": [], "tried": tried}

    return {"route": "abstract_only", "pdf": None, "supplements": [], "tried": tried}


ROOT = Path(__file__).resolve().parents[2]


def _file_entry(path: str) -> dict:
    p = Path(path)
    try:
        rel = str(p.resolve().relative_to(ROOT))
    except ValueError:
        rel = p.name                      # outside the repo: keep the name only, never a home-dir path
    return {"path": rel, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}


def write_manifest(run_dir: Path, recs: list[dict]) -> Path:
    """fulltext.json: which file was read for each paper, how it was obtained, its license and version, and
    its sha256. Committed in place of the PDFs: open-access files can be re-fetched with `meccog fetch` and
    checked against the hashes; hand-supplied (possibly paywalled) files are named and hashed, never shared."""
    rows = []
    for r in recs:
        ft = r["fulltext"]
        row = {"key": r.get("pmid") or r.get("doi"), "title": r["title"], "doi": r.get("doi"),
               "pmid": r.get("pmid"), "pmcid": r.get("pmcid"), "route": ft["route"],
               "license": ft.get("license"), "version": ft.get("version"), "host": ft.get("host"),
               "retracted": ft.get("retracted"), "tried": ft.get("tried", []),
               "pdf": _file_entry(ft["pdf"]) if ft.get("pdf") and Path(ft["pdf"]).exists() else None,
               "supplements": [_file_entry(x) for x in ft.get("supplements", []) if Path(x).exists()]}
        rows.append({k: v for k, v in row.items() if v not in (None, [])} | {"pdf": row["pdf"]})
    out = run_dir / "fulltext.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    return out


def run(papers: list[dict], run_dir: Path, pdf_dir: Path) -> list[dict]:
    out, missing = [], []
    for rec in papers:
        ft = fetch(rec, pdf_dir, run_dir / "pdfs")
        flag = "  RETRACTED" if ft.get("retracted") else ""
        print(f"  {ft['route']:<13} {rec.get('pmid') or rec.get('doi')}  {rec['title'][:60]}{flag}")
        out.append({**rec, "fulltext": ft})
        if ft["route"] == "abstract_only":
            missing.append(rec)
    write_manifest(run_dir, out)
    lines = ["# Papers extracted from the abstract only", "",
             f"Save each PDF into `{pdf_dir}` as `<PMID>.pdf` (supplements as `<PMID>_supp1.pdf`, ...), "
             "then rerun `meccog extract` for this hypothesis after deleting its "
             "`extractions/<PMID>.json`.", ""]
    for r in missing:
        doi = f"https://doi.org/{r['doi']}" if r.get("doi") else "no DOI"
        lines.append(f"- [ ] `{r.get('pmid')}.pdf` — {r['title']} ({r['year']}) — {doi}")
    (run_dir / "needs_pdf.md").write_text("\n".join(lines) + "\n")
    print(f"full text: {len(out) - len(missing)}/{len(out)} papers; {len(missing)} listed in needs_pdf.md")
    return out
