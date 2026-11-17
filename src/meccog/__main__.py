"""meccog CLI. Each step reads and writes files in runs/<H>/ so any step can be rerun on its own.

  meccog search   H1            candidates.jsonl, search_log.json
  meccog screen   H1            screen.jsonl                 (Claude, title + abstract)
  meccog frame    H1            frame.json: A, B, context as ontology terms (Claude + OLS/HGNC check)
  meccog fetch    H1            fulltext.json, pdfs/         (shortlist only)
  meccog extract  H1            extractions/<id>.json        (Claude, full PDF)
  meccog audit    H1            audit.json, audit_report.md, graph.json  (no Claude calls)
  meccog mira     H1            mira/: MIRA records (+ MecCog extension); edit these to fix a run
  meccog assemble H1            meccog_H1_submission.xlsx + validator output
  meccog all      H1            every step in order
  meccog queue                  runs/access_queue.csv: relevant but closed papers, across hypotheses
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import assemble, audit, entities, extract, mira, fulltext, hypotheses, queue, screen, search

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="meccog", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["search", "screen", "frame", "fetch", "extract", "audit", "mira", "assemble", "all",
                                     "resources", "queue"])
    ap.add_argument("hypothesis", nargs="?", help="H1..H5")
    ap.add_argument("--runs", type=Path, default=ROOT / "runs")
    ap.add_argument("--pdfs", type=Path, default=ROOT / "pdfs", help="hand-supplied PDFs (<PMID>.pdf)")
    ap.add_argument("--query", action="append", default=[], help="extra Europe PMC query (repeatable)")
    ap.add_argument("--no-model-queries", action="store_true")
    ap.add_argument("--no-openalex", action="store_true")
    ap.add_argument("--per-query", type=int, default=200)
    ap.add_argument("--max-papers", type=int, default=60, help="shortlist size before extraction")
    ap.add_argument("--min-relevance", type=float, default=0.5)
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--team", type=Path, default=ROOT / "team.toml")
    a = ap.parse_args(argv)

    if a.step == "resources":
        assemble.fetch_resources(force=True)
        return
    if a.step == "queue":
        queue.build(a.runs, a.pdfs, max(a.min_relevance, 0.6))
        return
    if not a.hypothesis:
        ap.error("hypothesis required")
    hyp = hypotheses.get(a.hypothesis)
    run_dir = a.runs / hyp["id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "hypothesis.json").write_text(json.dumps(hyp, indent=2))
    steps = ["search", "screen", "frame", "extract", "audit", "mira", "assemble"] if a.step == "all" else [a.step]

    for step in steps:
        print(f"== {step} {hyp['id']}: {hyp['short']}")
        if step == "search":
            search.run(hyp, run_dir, a.query, a.per_query, not a.no_model_queries, not a.no_openalex)
        elif step == "screen":
            screen.run(hyp, run_dir, workers=max(a.workers, 8))
        elif step == "frame":
            frame_path = run_dir / "frame.json"
            if frame_path.exists() and json.loads(frame_path.read_text()).get("edited_by_human"):
                print("  frame.json is marked edited_by_human; leaving it alone")
            else:
                f = entities.make_frame(hyp, run_dir, entities.Grounder(a.runs / "term_cache.json"))
                print(entities.frame_text(f))
        elif step in ("fetch", "extract"):
            short = screen.shortlist(run_dir, hyp, a.max_papers, a.min_relevance)
            papers = fulltext.run(short, run_dir, a.pdfs)
            if step == "extract":
                extract.run(hyp, papers, run_dir, a.effort, a.workers)
        elif step == "audit":
            audit.run(run_dir)
        elif step == "mira":
            mira.build(run_dir)
        elif step == "assemble":
            assemble.run(hyp, run_dir, a.team)


if __name__ == "__main__":
    main()
