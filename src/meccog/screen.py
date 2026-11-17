"""Step 2: screen candidates on title + abstract. Keeps primary experimental papers whose experiments bear
on the hypothesis's A (APOE genotype/isoform) -> B (the endosomal variable) link, for or against.

Resumable: each verdict is appended to screen.jsonl as it lands, and a rerun skips papers already
screened.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import claude

SYSTEM = """You screen papers for a literature extraction task. You get one mechanism hypothesis and one
paper's title and abstract.

Every hypothesis has the form: a change in system variable A (APOE4 vs APOE3 genotype or protein) causes
a change in system variable B (an endosomal/lysosomal property). Decide whether this paper is likely to
contain PRIMARY experimental findings bearing on that A -> B link, or on an intermediate step of it.

Include:
- experiments in any model system (human cells, iPSC-derived cells, APOE targeted-replacement or knock-in
  mice, cell lines, recombinant protein, human tissue or CSF). The hypothesis names human brain cells,
  but model-system findings are explicitly relevant.
- findings AGAINST the hypothesis, or showing no APOE4 vs APOE3 difference. Relevance is about the
  subject, not the direction of the result.
- papers whose main topic differs but which report an APOE4-vs-APOE3 comparison on B.

Exclude: reviews, commentaries and editorials (they hold no primary findings); papers on Alzheimer's
mechanisms generally with no APOE isoform comparison on the endosomal variable; purely clinical or genetic
association studies with no measurement of B.

relevance: 0 to 1. 0.9+ the paper directly tests A -> B; 0.6-0.8 it measures B or a close surrogate under
APOE4 vs APOE3, or a key intermediate step; 0.3-0.5 tangential, some chance of one usable finding;
below 0.3 not relevant."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["include", "relevance", "is_primary_research", "reason"],
    "properties": {
        "include": {"type": "boolean"},
        "relevance": {"type": "number"},
        "is_primary_research": {"type": "boolean"},
        "reason": {"type": "string", "description": "one sentence: what the paper measured that bears on A -> B"},
    },
}


def _one(hyp: dict, c: dict) -> dict:
    text = (f"HYPOTHESIS: {hyp['statement']}\n\nTITLE: {c['title']}\nYEAR: {c['year']}\n"
            f"PUBLICATION TYPES: {', '.join(c['pubtypes'])}\n\nABSTRACT: {c['abstract'] or '(none)'}")
    data, usage = claude.json_call(SYSTEM, [{"type": "text", "text": text}], SCHEMA,
                                   model=claude.SCREEN_MODEL, effort="low", max_tokens=8000)
    data["relevance"] = max(0.0, min(1.0, float(data["relevance"])))
    return {"key": c["pmid"] or c["doi"] or c["epmc_id"], **data, "model": usage["model"]}


def run(hyp: dict, run_dir: Path, workers: int = 8) -> Path:
    cands = [json.loads(l) for l in (run_dir / "candidates.jsonl").read_text().splitlines() if l]
    out = run_dir / "screen.jsonl"
    done = set()
    if out.exists():
        done = {json.loads(l)["key"] for l in out.read_text().splitlines() if l}
    todo = [c for c in cands if (c["pmid"] or c["doi"] or c["epmc_id"]) not in done]
    # Papers with no abstract can't be judged on one; the seed is always kept.
    print(f"screening {len(todo)} of {len(cands)} candidates ({len(done)} already screened)")
    with out.open("a") as fh, ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(_one, hyp, c): c for c in todo}
        for i, f in enumerate(as_completed(futs), 1):
            c = futs[f]
            try:
                rec = f.result()
            except Exception as e:  # keep going; the failure is recorded and retried on rerun
                print(f"  ! {c['title'][:70]}: {e}")
                continue
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}")
    return out


def shortlist(run_dir: Path, hyp: dict, k: int, min_relevance: float) -> list[dict]:
    """Candidates that passed screening, best first, with the seed always included."""
    cands = {(c["pmid"] or c["doi"] or c["epmc_id"]): c
             for c in map(json.loads, (run_dir / "candidates.jsonl").read_text().splitlines())}
    verdicts = [json.loads(l) for l in (run_dir / "screen.jsonl").read_text().splitlines() if l]
    keep = [v for v in verdicts
            if (v["include"] and v["is_primary_research"] and v["relevance"] >= min_relevance)
            or v["key"] == hyp["seed_pmid"]]
    keep.sort(key=lambda v: (v["key"] != hyp["seed_pmid"], -v["relevance"]))
    return [{**cands[v["key"]], "screen": v} for v in keep[:k] if v["key"] in cands]
