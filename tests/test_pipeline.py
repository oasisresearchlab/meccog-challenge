"""No Claude calls. Builds a one-page PDF, a fake extraction with one good and one bad finding, then runs
audit -> assemble -> the official validator."""

import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from meccog import assemble, audit, entities, hypotheses

TEXT = ("Results. Endosomal pH was significantly lower in ApoE4 astrocytes compared with ApoE3 astrocytes "
        "(Fig. 2B; n = 6, P < 0.01). ")


def _finding(**over):
    f = {"description": "In astrocytes from APOE4 targeted-replacement mice, endosomal pH was lower than in APOE3.",
         "quote": "Endosomal pH was significantly lower in ApoE4 astrocytes compared with ApoE3 astrocytes",
         "quote_page": 1, "summary": "(APOE3 -> APOE4) -> (lower endosomal pH in astrocytes)", "relevance": 0.9,
         "experimental_system": "Primary astrocytes from APOE3- and APOE4-TR mice; pHluorin ratio imaging.",
         "data_location": "Fig 2B", "effect_size": "qualitative", "effect_size_provenance": "visual_only",
         "p_value": "P < 0.01", "p_value_provenance": "printed_in_text", "sample_size": "n = 6",
         "sample_size_provenance": "printed_in_text", "directness_class": "1", "directness_strength": "1",
         "direction": "supports",
         "perturbed": {"variable": "APOE genotype", "maps_to": "A", "terms": [{"label": "APOE", "id": "HGNC:613"}]},
         "measured": {"variable": "endosomal pH", "maps_to": "B",
                      "terms": [{"label": "endosomal lumen acidification", "id": ""}]},
         "system_terms": [{"label": "astrocyte", "id": "CL:0000127"}, {"label": "Mus musculus", "id": "NCBITaxon:10090"}]}
    f.update(over)
    return f


@pytest.fixture
def run_dir(tmp_path):
    pdf = tmp_path / "p.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), TEXT, fontsize=10)
    doc.save(pdf)
    rd = tmp_path / "runs" / "H1"
    (rd / "extractions").mkdir(parents=True)
    paper = {"pmid": "29946028", "doi": "10.1073/pnas.1801612115", "title": "Test paper", "is_preprint": False,
             "abstract": "", "fulltext": {"route": "local", "pdf": str(pdf), "supplements": []}}
    bad = _finding(quote="ApoE4 strongly alkalinized the lysosome in every condition tested", p_value="~0.02",
                   p_value_provenance="printed_in_text")
    (rd / "extractions" / "29946028.json").write_text(json.dumps(
        {"paper": paper, "extraction": {"paper_notes": "test", "findings": [_finding(), bad], "excluded": []},
         "usage": {"model": "claude-opus-5-5"}}))
    (rd / "screen.jsonl").write_text(json.dumps({"key": "29946028", "model": "claude-opus-5-5"}) + "\n")
    (rd / "frame.json").write_text(json.dumps({
        "hypothesis": "H1", "statement": hypotheses.get("H1")["statement"],
        "A": {"variable": "APOE genotype", "terms": []}, "B": {"variable": "endosome pH", "terms": []},
        "context": [], "intermediates": []}))
    return rd


def test_normalizers():
    assert audit.norm_effect("-40 %") == "40%"
    assert audit.norm_effect("2.5-fold") == "2.5x"
    assert audit.norm_p("P < 0.001") == "<0.001"
    assert audit.norm_p("p = 0.03") == "0.03"
    assert audit.norm_n("n = 6") == "6"
    assert audit.norm_n("8 to 12") == "8-12"


def test_implied_class():
    f = _finding()
    assert entities.implied_class(f) == "1"
    f["perturbed"]["maps_to"] = "A_surrogate"
    f["measured"]["maps_to"] = "intermediate"
    assert entities.implied_class(f) == "4"


def test_audit_keeps_good_drops_bad(run_dir):
    audit.run(run_dir)
    a = json.loads((run_dir / "audit.json").read_text())[0]
    good, bad = a["findings"]
    assert good["keep"], good["errors"]
    assert good["finding"]["p_value"] == "<0.01" and good["finding"]["sample_size"] == "6"
    assert not bad["keep"]
    assert any("quote" in e for e in bad["errors"])
    assert any("estimated" in e for e in bad["errors"])
    assert (run_dir / "graph.json").exists()


def test_assemble_passes_official_validator(run_dir, tmp_path):
    audit.run(run_dir)
    team = tmp_path / "team.toml"
    team.write_text('team = "t"\nteam_leader = "A B"\nteam_leader_email = "a@b.org"\n'
                    'method_url = "https://github.com/oasisresearchlab/meccog-challenge"\nwho_did_the_work = "agent"\n')
    out = assemble.run(hypotheses.get("H1"), run_dir, team)
    res = subprocess.run([sys.executable, str(assemble.RES / "validate_submission.py"), str(out)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def _cells(path):
    import openpyxl
    return [tuple(r) for r in openpyxl.load_workbook(path)["Submission"].iter_rows(values_only=True)]


def _team(tmp_path):
    team = tmp_path / "team.toml"
    team.write_text('team = "t"\nteam_leader = "A B"\nteam_leader_email = "a@b.org"\n'
                    'method_url = "https://github.com/oasisresearchlab/meccog-challenge"\nwho_did_the_work = "agent"\n')
    return team


def test_mira_view_matches_audit_view_and_keeps_edits(run_dir, tmp_path):
    import shutil
    from meccog import mira
    (run_dir / "hypothesis.json").write_text(json.dumps(hypotheses.get("H1")))
    audit.run(run_dir)
    team = _team(tmp_path)
    hyp = hypotheses.get("H1")
    plain = assemble.run(hyp, run_dir, team, tmp_path / "plain.xlsx")
    mira.build(run_dir)
    view = assemble.run(hyp, run_dir, team, tmp_path / "view.xlsx")
    assert _cells(plain) == _cells(view)

    # hand-edit the dropped finding back in, and reword the kept one; both must survive two rebuilds
    idx = json.loads((run_dir / "mira" / "index.json").read_text())
    def path(slug):
        o = idx[slug]
        return run_dir / "mira" / "records" / o["nsid"] / f"{o['rkey']}.json"
    evs = sorted(s for s in idx if s.startswith("evd:"))
    for slug in evs:
        v = json.loads(path(slug).read_text())
        if v["meccogFinding"]["audit"]["status"] == "dropped":
            v["meccogFinding"]["audit"]["status"] = "kept"
            v["meccogFinding"]["pValue"], v["meccogFinding"]["pValueProvenance"] = "N/A", "not_found_in_source"
            v["provenance"].pop("excerpt", None)
        else:
            v["text"] = "EDITED " + v["text"]
        path(slug).write_text(json.dumps(v))
    mira.build(run_dir)
    mira.build(run_dir)
    rows = _cells(assemble.run(hyp, run_dir, team, tmp_path / "edited.xlsx"))
    findings = [r for r in rows[2:] if r[4] and "." in str(r[4])]
    assert len(findings) == 2
    assert any(str(r[5]).startswith("EDITED") for r in findings)
    idx = json.loads((run_dir / "mira" / "index.json").read_text())
    rels = [json.loads(p.read_text()) for p in (run_dir / "mira" / "records").glob("*.supports/*.json")]
    assert len(rels) == 2
    assert {r["subject"]["cid"] for r in rels} == {idx[s]["cid"] for s in evs}
