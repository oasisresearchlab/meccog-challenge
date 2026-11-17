"""Step 6: write the submission spreadsheet from the official template, then run the official validator.

Papers are ordered by their best kept finding's relevance (P1 = most relevant) and capped at 50.
The Method sheet is filled from team.toml; the commit SHA from this repository's HEAD; the model
versions from what the API reported on each call.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import openpyxl
import requests

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "resources"
BASE = "https://huggingface.co/spaces/MecCog/meccog-challenge/resolve/main/resources/"
RESOURCE_FILES = ["submission_template.xlsx", "validate_submission.py", "method_README_template.md",
                  "submission_example.xlsx", "Directness_of_evidence_v02.docx"]
STRENGTH = {"1": "1 very strong", "2": "2 strong", "3": "3 moderate", "4": "4 weak", "5": "5 very weak"}
MAX_PAPERS = 50


def fetch_resources(force: bool = False) -> None:
    RES.mkdir(exist_ok=True)
    for f in RESOURCE_FILES:
        if force or not (RES / f).exists():
            r = requests.get(BASE + f, timeout=60)
            r.raise_for_status()
            (RES / f).write_bytes(r.content)
            print(f"  fetched {f}")


def source_type(p: dict) -> str:
    if p.get("pmid"):
        return "PubMed preprint" if p.get("is_preprint") else "PubMed published"
    return "Other"


def commit_sha() -> str:
    try:
        sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--", "src", "skills"],
                                        text=True).strip()
        if dirty:
            print("  ! src/ or skills/ has uncommitted changes: the SHA will not pin what ran")
        return sha
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "N/A"


def run(hyp: dict, run_dir: Path, team_file: Path, out: Path | None = None) -> Path:
    fetch_resources()
    papers = []
    if (run_dir / "mira" / "index.json").exists():
        from . import mira
        print("  rendering from MIRA records (runs/<H>/mira)")
        for paper, kept in mira.load_for_sheet(run_dir):
            kept.sort(key=lambda f: (-f["relevance"], f["order"]))
            papers.append((max(f["relevance"] for f in kept), paper, kept))
    else:
        for a in json.loads((run_dir / "audit.json").read_text()):
            kept = [{**f["finding"], "order": f["n"]} for f in a["findings"] if f["keep"]]
            if kept:
                kept.sort(key=lambda f: (-f["relevance"], f["order"]))
                papers.append((max(f["relevance"] for f in kept), {**a["paper"],
                               "source_type": source_type(a["paper"])}, kept))
    # deterministic order: relevance, then published before preprint/other, then identifier
    papers.sort(key=lambda t: (-t[0], t[1]["source_type"] != "PubMed published",
                               str(t[1].get("pmid") or t[1].get("doi"))))
    if len(papers) > MAX_PAPERS:
        print(f"  {len(papers)} papers with findings; keeping the top {MAX_PAPERS}")
        papers = papers[:MAX_PAPERS]

    out = out or run_dir / f"meccog_{hyp['id']}_submission.xlsx"
    shutil.copy(RES / "submission_template.xlsx", out)
    wb = openpyxl.load_workbook(out)
    ws = wb["Submission"]
    ws["A2"] = hyp["statement"]
    r = 3
    for pi, (_, p, findings) in enumerate(papers, 1):
        ws.cell(r, 2, p.get("doi") or "N/A")
        ws.cell(r, 3, p["source_type"])
        ws.cell(r, 4, p.get("pmid") or "N/A")
        ws.cell(r, 5, f"P{pi}")
        r += 1
        for fi, f in enumerate(findings, 1):
            row = [None, None, None, None, f"P{pi}.F{fi}", f["description"], f["quote"],
                   f["summary"] or "N/A", round(f["relevance"], 2), f["experimental_system"], f["data_location"],
                   f["effect_size"], f["effect_size_provenance"], f["p_value"], f["p_value_provenance"],
                   f["sample_size"], f["sample_size_provenance"], int(f["directness_class"]),
                   STRENGTH[f["directness_strength"]]]
            for c, v in enumerate(row, 1):
                if v is not None:
                    ws.cell(r, c, v)
            r += 1

    team = tomllib.loads(team_file.read_text())
    models = set()
    for path in (run_dir / "extractions").glob("*.json"):
        models.add(json.loads(path.read_text())["usage"]["model"])
    for line in (run_dir / "screen.jsonl").read_text().splitlines():
        if line:
            models.add(json.loads(line)["model"])
    who = team.get("who_did_the_work", "agent")
    method = {"Team": team["team"], "Team leader": team["team_leader"],
              "Team leader email": team["team_leader_email"], "Method URL": team["method_url"],
              "Commit SHA": commit_sha(), "Who did the work": who,
              "Model and version": "; ".join(sorted(models)) or "N/A"}
    ms = wb["Method"]
    for row in range(1, ms.max_row + 1):
        label = ms.cell(row, 1).value
        if label in method:
            ms.cell(row, 2, method[label])
    wb.save(out)
    print(f"wrote {out}: {len(papers)} papers, {sum(len(t[2]) for t in papers)} findings")

    res = subprocess.run([sys.executable, str(RES / "validate_submission.py"), str(out)],
                         capture_output=True, text=True)
    (run_dir / "validator.txt").write_text(res.stdout + res.stderr)
    print(res.stdout[-3000:] + res.stderr[-1000:])
    return out
