"""Step 4: one Claude call per paper (the PDF, or the abstract when no full text was reachable), returning
findings in the challenge's fields. Raw JSON is kept per paper so the audit and the sheet can be rebuilt
without another API call, and a rerun skips papers already extracted.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import claude, entities

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/extract-findings/SKILL.md"

PROV = ["printed_in_text", "visual_only", "estimated_from_figure", "supplement_only",
        "not_found_in_source", "full_text_unavailable", "ambiguous_panel"]
PROV_P = [p for p in PROV if p != "estimated_from_figure"]

FINDING = {
    "type": "object", "additionalProperties": False,
    "required": ["description", "quote", "quote_page", "summary", "relevance", "experimental_system",
                 "data_location", "effect_size", "effect_size_provenance", "p_value", "p_value_provenance",
                 "sample_size", "sample_size_provenance", "directness_class", "directness_strength",
                 "direction", *entities.FINDING_FIELDS],
    "properties": {
        **entities.FINDING_FIELDS,
        "description": {"type": "string"},
        "quote": {"type": "string", "description": "verbatim from the paper, or N/A"},
        "quote_page": {"type": "integer", "description": "1-based PDF page of the quote; 0 if N/A"},
        "summary": {"type": "string"},
        "relevance": {"type": "number"},
        "experimental_system": {"type": "string"},
        "data_location": {"type": "string"},
        "effect_size": {"type": "string"},
        "effect_size_provenance": {"type": "string", "enum": PROV},
        "p_value": {"type": "string"},
        "p_value_provenance": {"type": "string", "enum": PROV_P},
        "sample_size": {"type": "string"},
        "sample_size_provenance": {"type": "string", "enum": PROV},
        "directness_class": {"type": "string", "enum": ["1", "2", "3", "4", "5"]},
        "directness_strength": {"type": "string", "enum": ["1", "2", "3", "4", "5"]},
        "direction": {"type": "string", "enum": ["supports", "contradicts", "no_effect", "mixed"]},
    },
}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["paper_notes", "findings", "excluded"],
    "properties": {
        "paper_notes": {"type": "string"},
        "findings": {"type": "array", "items": FINDING},
        "excluded": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["item", "reason"],
            "properties": {"item": {"type": "string"}, "reason": {"type": "string"}}}},
    },
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def system_prompt() -> str:
    return SKILL.read_text(encoding="utf-8") + "\n" + entities.PROMPT_ADDENDUM


def _one(hyp: dict, rec: dict, effort: str, frame: str) -> tuple[dict, dict]:
    pdf = rec["fulltext"]["pdf"]
    head = (f"HYPOTHESIS ({hyp['id']}): {hyp['statement']}\n\nFRAME:\n{frame}\n\n"
            f"PAPER: {rec['title']} ({rec['authors'][:120]}, {rec['journal']} {rec['year']}). "
            f"DOI {rec.get('doi')}, PMID {rec.get('pmid')}.\n\n")
    if pdf:
        supps = rec["fulltext"].get("supplements", [])
        budget = 30_000_000          # the API's request limit is 32 MB, base64 adds a third
        docs, used = [], 0
        for i, path in enumerate([pdf, *supps]):
            size = Path(path).stat().st_size * 4 // 3
            if used + size <= budget:
                docs.append(claude.pdf_block(Path(path)))
                used += size
            elif i == 0:            # main paper too big: send its text layer (figures are lost)
                import pymupdf
                with pymupdf.open(path) as d:
                    txt = "\n".join(f"[page {n}]\n{pg.get_text()}" for n, pg in enumerate(d, 1))
                docs.append(claude.text_block(txt[:600_000], "paper (text layer only; PDF too large)"))
                head += ("NOTE: the PDF was too large to send, so you have its text layer only: figures are "
                         "not visible, so do not estimate from figures.\n\n")
        supps = supps[:len(docs) - 1]
        content = docs
        if rec["fulltext"].get("version") == "submittedVersion":
            head += ("NOTE: this PDF is the PREPRINT of the paper, not the published version. Cite figure "
                     "panels as they appear in it.\n\n")
        note = (f"The first document is the paper; the {len(supps)} after it are its supplementary files "
                "(use supplement_only for values taken from them). ") if supps else ""
        content.append({"type": "text", "text": head + note +
                        "Extract the findings in this paper that bear on the hypothesis."})
    else:
        content = [claude.text_block(f"{rec['title']}\n\n{rec['abstract']}", "abstract"),
                   {"type": "text", "text": head + "Only the abstract was reachable (full text unavailable). "
                                                   "Extract what it states that bears on the hypothesis."}]
    return claude.json_call(system_prompt(), content, SCHEMA,
                            model=claude.EXTRACT_MODEL, effort=effort)


def run(hyp: dict, papers: list[dict], run_dir: Path, effort: str = "high", workers: int = 4) -> list[dict]:
    frame = entities.frame_text(json.loads((run_dir / "frame.json").read_text()))
    out_dir = run_dir / "extractions"
    out_dir.mkdir(exist_ok=True)
    todo = []
    for rec in papers:
        key = rec.get("pmid") or rec["doi"].replace("/", "_")
        if not (out_dir / f"{key}.json").exists():
            todo.append((key, rec))
    print(f"extracting {len(todo)} of {len(papers)} papers")
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(_one, hyp, rec, effort, frame): (key, rec) for key, rec in todo}
        for f in as_completed(futs):
            key, rec = futs[f]
            try:
                data, usage = f.result()
            except Exception as e:
                print(f"  ! {key}: {e}")
                continue
            for x in data["findings"]:
                x["relevance"] = max(0.0, min(1.0, float(x["relevance"])))
            (out_dir / f"{key}.json").write_text(json.dumps(
                {"paper": {k: rec.get(k) for k in ("pmid", "pmcid", "doi", "title", "authors", "journal",
                                                    "year", "is_preprint", "abstract", "screen", "fulltext")},
                 "extraction": data, "usage": usage, "effort": effort, "skill_sha256": sha(SKILL),
                 "frame_sha256": sha(run_dir / "frame.json")},
                indent=2, ensure_ascii=False))
            print(f"  {len(data['findings']):>2} findings  {key}  {rec['title'][:60]}")
    return [json.loads(p.read_text()) for p in sorted(out_dir.glob("*.json"))]
