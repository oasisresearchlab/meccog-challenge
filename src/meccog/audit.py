"""Step 5: check every finding against its source before it reaches the sheet.

Errors drop a finding; warnings are kept and listed in the report for a human to look at.

  - quote: verbatim coverage against the PDF text (or the abstract, for abstract-only papers).
    Below 0.90 the quote is not in the paper: an ERROR, because the rules treat that as fabrication.
  - printed values: a value coded printed_in_text should appear in the text layer. Missing is a WARNING
    (legends are usually text, but some PDFs render them as images).
  - estimate marks: `~` if and only if estimated_from_figure; never on a P value. ERROR.
  - value/provenance agreement: N/A goes with an "absent" code, a value with a "present" code. ERROR.
  - formats: normalized to the validator's shapes where the meaning is unambiguous (`-40%` -> `40%`,
    `2.5-fold` -> `2.5x`, `P < 0.001` -> `<0.001`, `n = 6` -> `6`); anything left unparseable is an ERROR.
  - data location: a bare `Fig 4` on a paper whose figures have panels is a WARNING.
  - directness: class 1 with strength 4-5 is an ERROR (the validator rejects it too).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import entities
from . import pdf as pdfmod

ABSENT = {"not_found_in_source", "full_text_unavailable", "ambiguous_panel"}
PRESENT = {"printed_in_text", "visual_only", "estimated_from_figure", "supplement_only"}
EST = r"~?\s*"
RE_EFFECT = [re.compile(EST + r"\d+(\.\d+)?%$"), re.compile(EST + r"\d+(\.\d+)?x$", re.I),
             re.compile(EST + r"[a-zA-Z]+\s*=\s*-?\d+(\.\d+)?$"), re.compile(r"qualitative$", re.I)]
RE_P = [re.compile(r"\d+(\.\d+)?([eE][-+]?\d+)?$"), re.compile(r"[<>]=?\s*\d+(\.\d+)?([eE][-+]?\d+)?$")]
RE_N = [re.compile(EST + r"\d+$"), re.compile(EST + r"\d+\s*-\s*\d+$")]


def _clean(v: str) -> str:
    v = (v or "").strip().replace("–", "-").replace("—", "-").replace("×", "x").replace("−", "-")
    return "N/A" if v.lower() in {"", "n/a", "na", "none", "not reported"} else v


def norm_effect(v: str) -> str:
    v = _clean(v)
    v = re.sub(r"^(~?)\s*[-+]\s*(?=\d)", r"\1", v)                       # sign -> description
    v = re.sub(r"(\d)\s*-?\s*fold$", r"\1x", v, flags=re.I)
    v = re.sub(r"(\d)\s+%$", r"\1%", v)
    v = re.sub(r"^(~?)\s*(cohen'?s\s*)?([a-zA-Z]+)\s*=\s*", lambda m: f"{m.group(1)}{m.group(3)}=", v, flags=re.I)
    return v


TOKEN = re.compile(r"~?\s*(\d+(\.\d+)?\s*%|\d+(\.\d+)?\s*x\b|[a-zA-Z]+\s*=\s*-?\d+(\.\d+)?)", re.I)


def repair_effect(f: dict, warnings: list) -> None:
    """The accepted forms (%, x, d=, qualitative) cannot hold every effect a paper prints, e.g. a pH
    difference in pH units, or 'A vs B' means. Rather than drop such a finding or convert the value
    (the rules forbid conversions), keep the first accepted token when the string starts with one;
    otherwise record 'qualitative' and move the paper's own numbers into the description verbatim."""
    v, prov = f["effect_size"], f["effect_size_provenance"]
    if v == "N/A" or any(r.match(v) for r in RE_EFFECT):
        return
    if re.match(r"^n/?a\b", v, re.I):
        f["effect_size"] = "N/A"
        if prov not in ABSENT:
            f["effect_size_provenance"] = "not_found_in_source"
        return
    m = TOKEN.match(v)
    if m:
        tok = norm_effect(m.group(0))
        if any(r.match(tok) for r in RE_EFFECT):
            f["effect_size"] = tok
            if v[m.end():].strip():
                f["description"] = f["description"].rstrip(". ") + f" (reported: {v})."
            warnings.append(f"effect size trimmed to {tok!r} from {v!r}")
            return
    f["effect_size"] = "qualitative"
    if prov == "estimated_from_figure":
        f["effect_size_provenance"] = "visual_only"
    elif prov in ABSENT:
        f["effect_size_provenance"] = "printed_in_text"
    f["description"] = f["description"].rstrip(". ") + f" (reported: {v.lstrip('~').strip()})."
    warnings.append(f"effect size {v!r} has no accepted form: recorded as qualitative, numbers kept in description")


def norm_p(v: str) -> str:
    v = _clean(v)
    v = re.sub(r"^p\s*(?=[<>=])", "", v, flags=re.I).strip()
    v = re.sub(r"^=\s*", "", v)
    v = re.sub(r"\s+", "", v) if re.match(r"^[<>]", v) else v
    v = re.sub(r"(\d)\s*[x×]\s*10\^?\s*(-?\d+)", r"\1e\2", v)
    return v


def norm_n(v: str) -> str:
    v = _clean(v)
    v = re.sub(r"^(~?)\s*n\s*=\s*", r"\1", v, flags=re.I)
    v = re.sub(r"(\d)\s*(to|-)\s*(\d)", r"\1-\3", v)
    return v


def _number(v: str) -> str | None:
    m = re.search(r"\d+(\.\d+)?", v)
    return m.group(0) if m else None


def _in_text(num: str, text_flat: str) -> bool:
    if num in text_flat:
        return True
    if num.startswith("0."):                       # ".001" style
        return num[1:] in text_flat
    return False


def check_stat(f: dict, field: str, shapes: list, is_p: bool, text_flat: str | None,
               errors: list, warnings: list):
    prov_field = f"{field}_provenance"
    v, prov = f[field], f[prov_field]
    if v == "N/A":
        if prov not in ABSENT:
            errors.append(f"{field}: N/A but provenance {prov}")
        return
    if prov in ABSENT:
        errors.append(f"{field}: value {v!r} but provenance {prov}")
    if not any(r.match(v) for r in shapes):
        errors.append(f"{field}: {v!r} is not in an accepted form")
    est = v.startswith("~")
    if is_p and (est or prov == "estimated_from_figure"):
        errors.append("p_value: estimated (never allowed)")
    elif est != (prov == "estimated_from_figure"):
        errors.append(f"{field}: '~' and estimated_from_figure must go together ({v!r}, {prov})")
    if prov == "printed_in_text" and text_flat is not None and v.lower() != "qualitative":
        num = _number(v)
        if num and not _in_text(num, text_flat):
            warnings.append(f"{field}: printed_in_text value {v!r} not found in the PDF text layer")


def audit_paper(rec: dict, grounder: "entities.Grounder | None" = None, min_quote: float = 0.90) -> dict:
    paper, ext = rec["paper"], rec["extraction"]
    pdf = paper["fulltext"]["pdf"]
    if pdf and Path(pdf).exists():
        pages = pdfmod.pages(Path(pdf))
        for sp in paper["fulltext"].get("supplements", []):
            pages += pdfmod.pages(Path(sp))
    else:
        pages = [f"{paper['title']}\n{paper.get('abstract', '')}"]
    pages_norm = [pdfmod.norm(p) for p in pages]
    text_flat = re.sub(r"\s+", "", "".join(pages))
    has_panels = bool(re.search(r"\(\s*[A-H]\s*\)|\b[A-H]\s*[,–-]\s*[A-H]\b|Fig(?:ure)?\.?\s*\d+[A-H]\b",
                                "".join(pages)))

    out = []
    retracted = bool(paper["fulltext"].get("retracted"))
    for i, f in enumerate(ext["findings"], 1):
        f = dict(f)
        errors, warnings = [], []
        f["effect_size"] = norm_effect(f["effect_size"])
        f["p_value"] = norm_p(f["p_value"])
        f["sample_size"] = norm_n(f["sample_size"])
        f["quote"] = _clean(f["quote"])
        for k in ("description", "summary", "experimental_system", "data_location"):
            f[k] = _clean(f[k])

        cov, where = (None, None)
        if f["quote"] == "N/A":
            warnings.append("no quote (figure-only result?)")
        else:
            cov, where = pdfmod.best_coverage(f["quote"], pages_norm, f.get("quote_page") or None)
            t = pdfmod.tier(cov)
            if cov < min_quote:
                errors.append(f"quote not found verbatim (coverage {cov:.2f}, {t})")
            elif t == "minor":
                warnings.append(f"quote near-verbatim (coverage {cov:.2f})")

        repair_effect(f, warnings)
        for k in ("p_value", "sample_size"):
            if f[k] == "N/A" and f[f"{k}_provenance"] not in ABSENT:
                f[f"{k}_provenance"] = "not_found_in_source"
        check_stat(f, "effect_size", RE_EFFECT, False, text_flat, errors, warnings)
        check_stat(f, "p_value", RE_P, True, text_flat, errors, warnings)
        check_stat(f, "sample_size", RE_N, False, text_flat, errors, warnings)

        if f["directness_class"] == "1" and f["directness_strength"] in {"4", "5"}:
            errors.append("directness: class 1 with strength 4-5 (scale read upside down?)")
        if f["directness_class"] == "5" and "other" not in f["description"].lower():
            warnings.append("directness class 5: description should say why no class fits")
        if "perturbed" in f:
            implied = entities.implied_class(f)
            if f["directness_class"] != "5" and implied != f["directness_class"]:
                warnings.append(f"directness class {f['directness_class']} but the entity mapping "
                                f"({f['perturbed']['maps_to']} -> {f['measured']['maps_to']}) implies {implied}")
            if grounder:
                f["perturbed"] = grounder.ground_var(f["perturbed"])
                f["measured"] = grounder.ground_var(f["measured"])
                f["system_terms"] = [grounder.ground(t) for t in f["system_terms"]]
                bad = [t for t in f["perturbed"]["terms"] + f["measured"]["terms"] + f["system_terms"]
                       if t["status"] == "id_rejected"]
                if bad:
                    warnings.append("ontology ids rejected (label mismatch): "
                                    + ", ".join(f"{t['proposed_id']} '{t['label']}'" for t in bad))
        if paper["fulltext"].get("version") == "submittedVersion":
            warnings.append("extracted from the preprint: check panels and numbers against the published paper")
        if retracted:
            errors.append("paper is retracted (PMC flag)")
        if has_panels and re.fullmatch(r"(Supplementary\s+)?Fig(ure)?\.?\s*\d+", f["data_location"], re.I):
            warnings.append("data location not at panel level")
        for k in ("description", "summary", "experimental_system", "data_location"):
            if f[k] == "N/A" and k != "summary":
                errors.append(f"{k} is empty")

        out.append({"n": i, "finding": f, "quote_coverage": cov, "quote_found_page": where,
                    "errors": errors, "warnings": warnings, "keep": not errors})
    return {"key": paper.get("pmid") or paper.get("doi"), "paper": paper,
            "paper_notes": ext["paper_notes"], "excluded": ext["excluded"], "findings": out}


def run(run_dir: Path) -> Path:
    recs = [json.loads(p.read_text()) for p in sorted((run_dir / "extractions").glob("*.json"))]
    grounder = entities.Grounder(run_dir.parent / "term_cache.json")
    audited = [audit_paper(r, grounder) for r in recs]
    grounder.save()
    if (run_dir / "frame.json").exists():
        entities.export_graph(run_dir, audited)
    (run_dir / "audit.json").write_text(json.dumps(audited, indent=2, ensure_ascii=False))

    lines = ["# Audit report", ""]
    tot = sum(len(a["findings"]) for a in audited)
    kept = sum(f["keep"] for a in audited for f in a["findings"])
    lines += [f"{len(audited)} papers, {tot} findings extracted, {kept} kept, {tot - kept} dropped.", ""]
    for a in audited:
        p = a["paper"]
        lines += [f"## {p['title']}", f"PMID {p.get('pmid')} · DOI {p.get('doi')} · full text: "
                  f"{p['fulltext']['route']}", "", a["paper_notes"], ""]
        for f in a["findings"]:
            mark = "kept" if f["keep"] else "DROPPED"
            lines.append(f"- **F{f['n']} ({mark})** {f['finding']['description']}")
            for e in f["errors"]:
                lines.append(f"  - error: {e}")
            for w in f["warnings"]:
                lines.append(f"  - warning: {w}")
        lines.append("")
    (run_dir / "audit_report.md").write_text("\n".join(lines))
    print(f"audit: {kept}/{tot} findings kept across {len(audited)} papers -> {run_dir / 'audit_report.md'}")
    return run_dir / "audit.json"
