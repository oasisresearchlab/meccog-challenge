"""Entity layer: the bridge between findings and the hypothesis.

Every MecCog hypothesis is "a change in A causes a change in B". The *frame* grounds A, B and the intended
context (cell types, organism) in ontology terms once per hypothesis. Each finding then says what it
actually perturbed and measured, and maps each to the frame: A or a surrogate of A, B or a surrogate of B.
That mapping is the finding-to-hypothesis bridge, and it implies a directness class:

    perturbed A,  measured B   -> 1     perturbed A', measured B   -> 2
    perturbed A,  measured B'  -> 3     perturbed A', measured B'  -> 4      anything else -> 5

so the audit can check the model's stated class against its own mapping.

Identifiers use the schemes the Proto-OKN graphs key on (GO, CL, UBERON, NCBITaxon, CHEBI, MONDO, PR,
HGNC), so findings join to OKN without a mapping step. No identifier is trusted as written: each is looked
up (OLS4 for OBO ontologies, HGNC REST for genes) and kept only if its label matches; otherwise the id is
re-resolved by exact label search, and failing that dropped, leaving the label alone.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import requests

from . import claude
from .search import UA

OLS = "https://www.ebi.ac.uk/ols4/api"
HGNC = "https://rest.genenames.org"
PREFIXES = {"GO", "CL", "UBERON", "NCBITAXON", "CHEBI", "MONDO", "PR", "HGNC", "HP", "SO"}
CANON = {"NCBITAXON": "NCBITaxon"}


def canon(curie: str) -> str:
    """Canonical CURIE prefix casing (OLS returns e.g. 'hgnc:659')."""
    if ":" not in curie:
        return curie
    pre, local = curie.split(":", 1)
    return f"{CANON.get(pre.upper(), pre.upper())}:{local}"

TERM = {"type": "object", "additionalProperties": False, "required": ["label", "id"],
        "properties": {"label": {"type": "string"},
                       "id": {"type": "string", "description": "CURIE, e.g. GO:0048388, CL:0000127, "
                              "HGNC:613, NCBITaxon:10090; empty string if unsure"}}}
VAR = {"type": "object", "additionalProperties": False, "required": ["variable", "terms"],
       "properties": {"variable": {"type": "string"}, "terms": {"type": "array", "items": TERM}}}

FRAME_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["A", "B", "context", "intermediates"],
    "properties": {
        "A": VAR, "B": VAR,
        "context": {"type": "array", "items": TERM, "description": "intended cell types, organism, conditions"},
        "intermediates": {"type": "array", "items": VAR,
                          "description": "molecules or processes the hypothesis implies lie between A and B"},
    },
}

FRAME_SYSTEM = """You decompose one mechanism hypothesis of the form "a change in A causes a change in B"
into ontology-grounded variables, for linking experimental findings to it.

A is the APOE genotype/protein variable. B is the endosomal/lysosomal variable. Give each a short
variable phrase and 1-4 ontology terms: genes as HGNC (HGNC:613 is APOE), proteins as PR where useful,
compartments and processes as GO (cellular component / biological process), cell types as CL,
organism as NCBITaxon, chemicals as CHEBI, disease as MONDO. Give the CURIE only when you are confident
of it; otherwise an empty string and the exact term label (it will be looked up). Context: the intended
cell types, organism, and condition qualifiers in the hypothesis. Intermediates: named molecules or
processes the hypothesis implies between A and B (e.g. ARF6, receptor recycling), if any."""

# Fields added to each extracted finding (see extract.FINDING).
FINDING_FIELDS = {
    "perturbed": {"type": "object", "additionalProperties": False, "required": ["variable", "maps_to", "terms"],
                  "properties": {"variable": {"type": "string", "description": "what the experiment varied"},
                                 "maps_to": {"type": "string", "enum": ["A", "A_surrogate", "other"]},
                                 "terms": {"type": "array", "items": TERM}}},
    "measured": {"type": "object", "additionalProperties": False, "required": ["variable", "maps_to", "terms"],
                 "properties": {"variable": {"type": "string", "description": "what the experiment measured"},
                                "maps_to": {"type": "string", "enum": ["B", "B_surrogate", "intermediate", "other"]},
                                "terms": {"type": "array", "items": TERM}}},
    "system_terms": {"type": "array", "items": TERM,
                     "description": "organism, cell type, tissue of the experimental system"},
}

PROMPT_ADDENDUM = """
## Entities: linking the finding to the hypothesis frame

The user message gives the hypothesis FRAME: its A and B variables, intended context, and intermediates,
with ontology terms. For each finding also fill:

- **perturbed** — what the experiment varied, and `maps_to`: `A` if it is the frame's A itself (APOE4 vs
  APOE3 genotype, isoform-expressing cells, recombinant apoE4 vs apoE3, carriers vs non-carriers);
  `A_surrogate` if it varied something assumed to stand in for A; `other` if neither.
- **measured** — what was measured, and `maps_to`: `B` if it is the frame's B itself; `B_surrogate` if it
  is a readout assumed to reflect B; `intermediate` if it is one of the frame's intermediates; `other`.
- **terms** for each, and **system_terms** for organism / cell type / tissue, using the same ontologies
  as the frame (GO, CL, UBERON, NCBITaxon, CHEBI, HGNC, PR, MONDO). Give a CURIE only when confident;
  otherwise an empty id with the exact term label. Ids are checked against the ontologies.

Your directness_class must agree with this mapping: A+B is class 1, A_surrogate+B class 2, A+B_surrogate
class 3, A_surrogate+B_surrogate class 4. A measured intermediate is a surrogate of B for the class.
"""


def implied_class(f: dict) -> str:
    a = f["perturbed"]["maps_to"]
    b = f["measured"]["maps_to"]
    b = "B_surrogate" if b == "intermediate" else b
    return {("A", "B"): "1", ("A_surrogate", "B"): "2", ("A", "B_surrogate"): "3",
            ("A_surrogate", "B_surrogate"): "4"}.get((a, b), "5")


# ---------- grounding ---------------------------------------------------------------------------

class Grounder:
    """Look up CURIEs and labels, with an on-disk cache shared across runs."""

    def __init__(self, cache: Path):
        self.path = cache
        self.cache = json.loads(cache.read_text()) if cache.exists() else {}
        self.lock = threading.Lock()

    def save(self):
        self.path.write_text(json.dumps(self.cache, indent=1, sort_keys=True))

    def _memo(self, key: str, fn):
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        try:
            val = fn()
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return None                       # network trouble: don't cache, try again next time
        with self.lock:
            self.cache[key] = val
        return val

    def label_of(self, curie: str) -> list[str] | None:
        """[label, *synonyms] for a CURIE, or [] if it does not exist."""
        prefix = curie.split(":")[0].upper()
        if prefix == "HGNC":
            def f():
                r = requests.get(f"{HGNC}/fetch/hgnc_id/{curie}", headers={**UA, "Accept": "application/json"},
                                 timeout=30)
                docs = r.json()["response"]["docs"]
                return [docs[0]["symbol"], docs[0]["name"], *docs[0].get("alias_symbol", [])] if docs else []
        else:
            def f():
                onto = "ncbitaxon" if prefix == "NCBITAXON" else prefix.lower()
                r = requests.get(f"{OLS}/ontologies/{onto}/terms", params={"obo_id": curie}, headers=UA, timeout=30)
                if r.status_code == 404:
                    return []
                terms = (r.json().get("_embedded") or {}).get("terms", [])
                return [terms[0]["label"], *(terms[0].get("synonyms") or [])] if terms else []
        return self._memo(f"id:{curie}", f)

    def search(self, label: str, prefix: str | None) -> str | None:
        """CURIE whose label or synonym matches exactly, or None."""
        def f():
            if prefix == "HGNC":
                r = requests.get(f"{HGNC}/fetch/symbol/{label}", headers={**UA, "Accept": "application/json"},
                                 timeout=30)
                docs = r.json()["response"]["docs"]
                return docs[0]["hgnc_id"] if docs else ""
            params = {"q": label, "exact": "true", "rows": 5, "queryFields": "label,synonym"}
            if prefix:
                params["ontology"] = "ncbitaxon" if prefix == "NCBITAXON" else prefix.lower()
            docs = requests.get(f"{OLS}/search", params=params, headers=UA, timeout=30).json()["response"]["docs"]
            docs = [d for d in docs if d.get("obo_id") and (not prefix or d["obo_id"].upper().startswith(prefix + ":"))
                    and d.get("is_defining_ontology", True)]
            return canon(docs[0]["obo_id"]) if docs else ""
        return self._memo(f"label:{prefix}:{label.lower()}", f) or None

    def ground(self, term: dict) -> dict:
        label, curie = term["label"].strip(), canon(term["id"].strip())
        prefix = curie.split(":")[0].upper() if ":" in curie else None
        if curie and prefix in PREFIXES:
            names = self.label_of(curie)
            if names is None:
                return {**term, "status": "unchecked"}
            if names and any(n.lower() == label.lower() for n in names):
                return {"label": label, "id": curie, "status": "verified"}
            found = self.search(label, prefix)
            if found:
                return {"label": label, "id": found, "status": "corrected", "proposed_id": curie}
            return {"label": label, "id": "", "status": "id_rejected", "proposed_id": curie}
        found = self.search(label, prefix)
        return {"label": label, "id": found or "", "status": "resolved" if found else "label_only"}

    def ground_var(self, v: dict) -> dict:
        return {**v, "terms": [self.ground(t) for t in v["terms"]]}


# ---------- frame -------------------------------------------------------------------------------

def make_frame(hyp: dict, run_dir: Path, grounder: Grounder) -> dict:
    data, usage = claude.json_call(FRAME_SYSTEM, [{"type": "text", "text": hyp["statement"]}], FRAME_SCHEMA,
                                   model=claude.EXTRACT_MODEL, effort="high", max_tokens=16000)
    frame = {"hypothesis": hyp["id"], "statement": hyp["statement"],
             "A": grounder.ground_var(data["A"]), "B": grounder.ground_var(data["B"]),
             "context": [grounder.ground(t) for t in data["context"]],
             "intermediates": [grounder.ground_var(v) for v in data["intermediates"]],
             "model": usage["model"], "edited_by_human": False}
    grounder.save()
    (run_dir / "frame.json").write_text(json.dumps(frame, indent=2, ensure_ascii=False))
    return frame


def frame_text(frame: dict) -> str:
    def terms(ts):
        return ", ".join(f"{t['label']} ({t['id']})" if t.get("id") else t["label"] for t in ts)
    lines = [f"A: {frame['A']['variable']} [{terms(frame['A']['terms'])}]",
             f"B: {frame['B']['variable']} [{terms(frame['B']['terms'])}]",
             f"Intended context: {terms(frame['context'])}"]
    for v in frame["intermediates"]:
        lines.append(f"Intermediate: {v['variable']} [{terms(v['terms'])}]")
    return "\n".join(lines)


# ---------- graph export ------------------------------------------------------------------------

def export_graph(run_dir: Path, audited: list[dict]) -> Path:
    """Hypothesis -> variables -> findings -> papers, as plain JSON nodes and edges keyed by CURIEs, for
    joining to OKN or loading into a discourse graph."""
    frame = json.loads((run_dir / "frame.json").read_text())
    nodes, edges = [], []
    h = f"hyp:{frame['hypothesis']}"
    nodes.append({"id": h, "type": "Hypothesis", "text": frame["statement"]})
    for role in ("A", "B"):
        vid = f"{h}/{role}"
        nodes.append({"id": vid, "type": "Variable", "role": role, "text": frame[role]["variable"],
                      "terms": frame[role]["terms"]})
        edges.append({"s": h, "p": "has_variable", "o": vid})
    for a in audited:
        p = a["paper"]
        pid = f"doi:{p['doi']}" if p.get("doi") else f"pmid:{p.get('pmid')}"
        nodes.append({"id": pid, "type": "Paper", "title": p["title"], "pmid": p.get("pmid"), "doi": p.get("doi")})
        for f in a["findings"]:
            if not f["keep"]:
                continue
            x = f["finding"]
            fid = f"{pid}#F{f['n']}"
            nodes.append({"id": fid, "type": "Finding", "text": x["description"], "summary": x["summary"],
                          "direction": x["direction"], "directness_class": x["directness_class"],
                          "directness_strength": x["directness_strength"], "data_location": x["data_location"],
                          "system_terms": x.get("system_terms", [])})
            edges.append({"s": fid, "p": "reported_in", "o": pid})
            rel = {"supports": "supports", "contradicts": "opposes", "no_effect": "opposes", "mixed": "informs"}
            edges.append({"s": fid, "p": rel.get(x["direction"], "informs"), "o": h})
            for side, var in (("perturbed", "A"), ("measured", "B")):
                if side not in x:
                    continue
                e = x[side]
                edges.append({"s": fid, "p": side, "o": f"{h}/{var}", "maps_to": e["maps_to"],
                              "variable": e["variable"], "terms": e["terms"]})
    out = run_dir / "graph.json"
    out.write_text(json.dumps({"nodes": nodes, "edges": edges}, indent=1, ensure_ascii=False))
    return out
