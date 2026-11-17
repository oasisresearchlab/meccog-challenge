"""The run as MIRA records (RRGI draft lexicons, `tech.scios.rrgi.*`), with a MecCog extension for what MIRA
has no field for. The records are the place to audit and fix a run; the spreadsheet is rendered from them.

    runs/<H>/mira/records/<nsid>/<rkey>.json   one record each
    runs/<H>/mira/index.json                   slug -> nsid, rkey, uri, cid, edit fingerprint
    runs/<H>/mira/records.md                   the graph, readable

Records
    claim         the hypothesis, with its frame's A/B/context terms as contextEntities
    evidence      one per finding: text = finding description, description = experimental system,
                  provenance.excerpt = verbatim quote, anchorLocations = where the quote is on the page,
                  plotReading = any value estimated from a figure, contextEntities = what was perturbed /
                  measured / the system, as ontology URIs. The `meccogFinding` object carries the rest.
    source        one per paper (`meccogSource` carries PMID, MecCog source type, full-text route)
    entity        one per cited figure panel or table (kind figurePanel / table)
    supports / opposes   evidence -> claim; strength from directness d, warrant from the class and mapping
    sourceDocument       evidence -> source;  observationBase   evidence -> entity
    protocol      this pipeline at its commit;  bundle   everything

Editing: change any record's JSON by hand, then rerun `meccog mira <H>`. An edited record keeps its content
(detected by fingerprint, ignoring the strongRefs inside it), its CID is recomputed, and every relation that
points at it is re-pinned. A finding the audit dropped is in the bundle with `meccogFinding.audit.status`
"dropped" and no supports/opposes edge; set it to "kept" to bring it back. `meccog assemble` renders the sheet
from these records whenever they exist.

The extension is specified in lexicons/org.oasisresearchlab.meccog.defs.json and docs/MIRA-EXTENSION.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import atproto

NS = "tech.scios.rrgi"
EXT = "org.oasisresearchlab.meccog.defs"
DID = "did:web:example.org"            # placeholder until records are published to a PDS
ROOT = Path(__file__).resolve().parents[2]

STRENGTH = {"1": "strong", "2": "strong", "3": "moderate", "4": "weak", "5": "weak"}
D_WORD = {"1": "very strong", "2": "strong", "3": "moderate", "4": "weak", "5": "very weak"}
CLASS_WORD = {"1": "direct", "2": "indirect input", "3": "indirect output",
              "4": "indirect input and output", "5": "other"}
REF_FIELDS = ("subject", "object", "members", "source")
OBO = re.compile(r"^(GO|CL|UBERON|NCBITaxon|CHEBI|MONDO|PR|HP|SO):(\d+)$")


def term_uri(curie: str) -> str | None:
    if not curie:
        return None
    m = OBO.match(curie)
    if m:
        return f"http://purl.obolibrary.org/obo/{m.group(1)}_{m.group(2)}"
    if curie.upper().startswith("HGNC:"):
        return f"https://identifiers.org/hgnc:{curie.split(':', 1)[1]}"
    return None


def entities_from_terms(terms: list[dict], entity_type: str) -> list[dict]:
    out = []
    for t in terms:
        e = {"name": t["label"], "entityType": entity_type}
        uri = term_uri(t.get("id", ""))
        if uri:
            e["uri"] = uri
        out.append(e)
    return out


def _fingerprint(value: dict) -> str:
    return atproto.cid({k: v for k, v in value.items() if k not in REF_FIELDS})


def _s(x) -> str:
    """Decimals as strings: DAG-CBOR has no floats."""
    if isinstance(x, float):
        return f"{x:.2f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))
    return str(x)


# ---------- panels ------------------------------------------------------------------------------

PANEL_RE = re.compile(r"(Supplementary\s+|Suppl?\.?\s+|Extended Data\s+)?(Fig(?:ure)?|Table)s?\.?\s*(S?\d+)"
                      r"([A-Za-z](?:\s*[-–]\s*[A-Za-z])?)?", re.I)


def panels(location: str) -> list[dict]:
    """'Fig 2A-D, Supplementary Fig 3B' -> [{kind, figure, panel, text}, ...]."""
    out = []
    for m in PANEL_RE.finditer(location):
        supp, kind, num, panel = m.groups()
        is_table = kind.lower().startswith("table")
        fig = ("S" + num.lstrip("Ss")) if supp and not num.upper().startswith("S") else num
        label = ("Table " if is_table else "Figure ") + fig
        p = {"kind": "table" if is_table else "figurePanel", "figure": fig, "text": label}
        if panel and not is_table:
            p["panel"] = re.sub(r"\s+", "", panel).replace("–", "-").upper()
            p["text"] += f", panel {p['panel']}"
        out.append(p)
    return out


# ---------- quote boxes -------------------------------------------------------------------------

def anchor(pdf: str | None, quote: str, page_hint: int | None) -> tuple[list[dict], str] | None:
    """Boxes (PDF points, origin top-left) for a quote: the whole quote, else its sentence segments."""
    if not pdf or not quote or quote == "N/A" or not Path(pdf).exists():
        return None
    import pymupdf
    q = re.sub(r"\s+", " ", quote).strip()
    segs = [s.strip() for s in re.split(r"[.;:]\s+", q) if len(s.strip()) > 25]
    with pymupdf.open(pdf) as doc:
        order = list(range(1, doc.page_count + 1))
        if page_hint and 1 <= page_hint <= doc.page_count:
            order.remove(page_hint)
            order.insert(0, page_hint)
        for mode, cands in (("normalized", [q]), ("split", segs)):
            found = {}
            for cand in cands:
                for p in order:
                    rects = doc[p - 1].search_for(cand)
                    if rects:
                        found.setdefault(p, []).extend(
                            {"x": round(r.x0), "y": round(r.y0), "w": max(1, round(r.width)),
                             "h": max(1, round(r.height))} for r in rects)
                        break
            if found:
                return [{"page": p, "boxes": b} for p, b in sorted(found.items())], mode
    return None


# ---------- build -------------------------------------------------------------------------------

class Builder:
    def __init__(self, run_dir: Path):
        self.dir = run_dir / "mira"
        self.rec_dir = self.dir / "records"
        idx_path = self.dir / "index.json"
        self.old = json.loads(idx_path.read_text()) if idx_path.exists() else {}
        meta = self.old.get("_meta", {})
        self.created_at = meta.get("createdAt") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.nodes: dict[str, dict] = {}      # slug -> {kind, value}
        self.edges: list[tuple[str, str, str, dict]] = []   # (kind, subj slug, obj slug, extra)
        self.edited: list[str] = []

    def node(self, slug: str, kind: str, value: dict):
        self.nodes[slug] = {"kind": kind, "value": {"$type": f"{NS}.{kind}", **value,
                                                     "createdAt": self.created_at}}

    def edge(self, kind: str, subj: str, obj: str, **extra):
        self.edges.append((kind, subj, obj, extra))

    def _existing(self, slug: str, kind: str) -> dict | None:
        """The on-disk value if a person edited it since the last build, else None."""
        o = self.old.get(slug)
        if not o:
            return None
        p = self.rec_dir / f"{NS}.{kind}" / f"{o['rkey']}.json"
        if not p.exists():
            return None
        cur = json.loads(p.read_text())
        if o.get("edited") or _fingerprint(cur) != o["fingerprint"]:
            self.edited.append(slug)       # once edited, always kept: delete the record file to regenerate it
            return cur
        return None

    def resolve_nodes(self):
        """Apply hand edits to nodes before edges are derived from them."""
        for slug, n in self.nodes.items():
            cur = self._existing(slug, n["kind"])
            if cur is not None:
                n["value"] = {**cur, "createdAt": n["value"]["createdAt"]}
                n["generated_fp"] = None

    def write(self, bundle_name: str, bundle_desc: str) -> dict:
        index = {"_meta": {"createdAt": self.created_at, "did": DID}}
        out: dict[str, dict] = {}

        def put(slug: str, kind: str, value: dict, generated: dict | None = None):
            rkey = atproto.tid(self.created_at, slug)
            nsid = f"{NS}.{kind}"
            c = atproto.cid(value)
            uri = f"at://{DID}/{nsid}/{rkey}"
            (self.rec_dir / nsid).mkdir(parents=True, exist_ok=True)
            (self.rec_dir / nsid / f"{rkey}.json").write_text(json.dumps(value, indent=2, ensure_ascii=False))
            fp_src = generated if generated is not None else value
            index[slug] = {"nsid": nsid, "rkey": rkey, "uri": uri, "cid": c, "fingerprint": _fingerprint(fp_src)}
            if slug in self.edited:
                index[slug]["edited"] = True
            out[slug] = {"kind": kind, "value": value, "uri": uri, "cid": c}

        def ref(slug):
            return {"uri": out[slug]["uri"], "cid": out[slug]["cid"]}

        if self.rec_dir.exists():                         # stale records from removed slugs go away
            for p in self.rec_dir.rglob("*.json"):
                p.unlink()

        # nodes that other nodes point at first (entity -> source), then the rest
        order = sorted(self.nodes, key=lambda s: (self.nodes[s]["kind"] != "source", s))
        for slug in order:
            n = self.nodes[slug]
            v = dict(n["value"])
            if n["kind"] == "entity" and "_source" in v:
                src = v.pop("_source")
                if src in out:
                    v["source"] = ref(src)
            put(slug, n["kind"], v)

        for kind, s, o, extra in self.edges:
            slug = f"{kind}:{s}->{o}"
            gen = {"$type": f"{NS}.{kind}", "subject": ref(s), "object": ref(o), **extra,
                   "createdAt": self.created_at}
            cur = self._existing(slug, kind)
            value = {**cur, "subject": ref(s), "object": ref(o)} if cur is not None else gen
            put(slug, kind, value, generated=gen if cur is None else value)

        members = [ref(s) for s in out]
        put("bundle", "bundle", {"$type": f"{NS}.bundle", "name": bundle_name, "description": bundle_desc,
                                 "members": members, "createdAt": self.created_at})
        (self.dir / "index.json").write_text(json.dumps(index, indent=1))
        return out


def build(run_dir: Path) -> Path:
    audited = json.loads((run_dir / "audit.json").read_text())
    frame = json.loads((run_dir / "frame.json").read_text())
    hyp = json.loads((run_dir / "hypothesis.json").read_text())
    b = Builder(run_dir)

    try:
        sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sha = "unknown"
    b.node("protocol", "protocol", {
        "text": "Find papers bearing on a mechanism hypothesis and extract their experimental findings with "
                "verbatim quotes, panel-level locations, statistics with provenance, and directness, each "
                "audited against the source.",
        "description": f"meccog pipeline, https://github.com/oasisresearchlab/meccog-challenge at commit {sha}.",
        "provenance": {"wasGeneratedBy": "manualAuthoring"}})

    b.node("hyp", "claim", {
        "text": hyp["statement"],
        "contextEntities": (entities_from_terms(frame["A"]["terms"], "A: " + frame["A"]["variable"])
                            + entities_from_terms(frame["B"]["terms"], "B: " + frame["B"]["variable"])
                            + entities_from_terms(frame["context"], "intended context")),
        "tags": ["meccog", hyp["id"], "hypothesis"],
        "localLabel": {"label": "Hypothesis", "baseType": f"{NS}.claim"},
        "meccogHypothesis": {"$type": f"{EXT}#hypothesis", "id": hyp["id"], "seedPmid": hyp["seed_pmid"],
                             "variableA": frame["A"]["variable"], "variableB": frame["B"]["variable"]},
    })

    for a in audited:
        p = a["paper"]
        pkey = str(p.get("pmid") or p.get("doi")).replace("/", "_")
        src = f"src:{pkey}"
        ft = p["fulltext"]
        meccog_type = ("PubMed preprint" if p.get("is_preprint") else "PubMed published") if p.get("pmid") else "Other"
        sv = {"text": f"{p.get('authors', '')[:200]} ({p.get('year')}). {p['title']}. {p.get('journal') or ''}".strip(),
              "sourceType": "preprint" if p.get("is_preprint") else "paper",
              "meccogSource": {"$type": f"{EXT}#source", "pmid": str(p.get("pmid") or "N/A"),
                               "sourceType": meccog_type, "fullTextRoute": ft["route"],
                               "fullTextVersion": ft.get("version") or "unknown",
                               "retracted": bool(ft.get("retracted"))}}
        if p.get("doi"):
            sv["doi"] = p["doi"]
            sv["url"] = f"https://doi.org/{p['doi']}"
        b.node(src, "source", sv)

        for f in a["findings"]:
            x = f["finding"]
            evd = f"evd:{pkey}:F{f['n']}"
            ctx = []
            if "perturbed" in x:
                ctx += entities_from_terms(x["perturbed"]["terms"] or [{"label": x["perturbed"]["variable"], "id": ""}],
                                           f"perturbed ({x['perturbed']['maps_to']})")
                ctx += entities_from_terms(x["measured"]["terms"] or [{"label": x["measured"]["variable"], "id": ""}],
                                           f"measured ({x['measured']['maps_to']})")
                ctx += entities_from_terms(x.get("system_terms", []), "experimental system")
            prov = {"wasGeneratedBy": "aiAssistedExtraction"}
            if x["quote"] != "N/A":
                prov["excerpt"] = x["quote"]
                loc = anchor(ft.get("pdf"), x["quote"], f.get("quote_found_page") or x.get("quote_page"))
                if loc:
                    prov["anchorLocations"], prov["anchorLocationsMode"] = loc
            ext = {"$type": f"{EXT}#finding", "hypothesis": hyp["id"], "extractionOrder": f["n"],
                   "summary": x["summary"], "relevance": _s(x["relevance"]), "dataLocation": x["data_location"],
                   "effectSize": x["effect_size"], "effectSizeProvenance": x["effect_size_provenance"],
                   "pValue": x["p_value"], "pValueProvenance": x["p_value_provenance"],
                   "sampleSize": x["sample_size"], "sampleSizeProvenance": x["sample_size_provenance"],
                   "directnessClass": x["directness_class"], "directnessStrength": x["directness_strength"],
                   "direction": x["direction"],
                   "audit": {"status": "kept" if f["keep"] else "dropped", "errors": f["errors"],
                             "warnings": f["warnings"],
                             "quoteCoverage": _s(round(f["quote_coverage"], 3)) if f["quote_coverage"] is not None else "N/A"}}
            if "perturbed" in x:
                ext["perturbed"] = {"variable": x["perturbed"]["variable"], "mapsTo": x["perturbed"]["maps_to"]}
                ext["measured"] = {"variable": x["measured"]["variable"], "mapsTo": x["measured"]["maps_to"]}
            ev = {"text": x["description"], "description": x["experimental_system"],
                  "contextEntities": ctx, "provenance": prov,
                  "tags": ["meccog", hyp["id"], x["direction"], f"directness-{x['directness_class']}"]
                          + ([] if f["keep"] else ["audit-dropped"]),
                  "meccogFinding": ext}
            if "estimated_from_figure" in (x["effect_size_provenance"], x["sample_size_provenance"]):
                ev["plotReading"] = True
            b.node(evd, "evidence", ev)
            b.edge("sourceDocument", evd, src)
            for pn in panels(x["data_location"]):
                ent = f"ent:{pkey}:{pn['text']}"
                if ent not in b.nodes:
                    ev_ent = {"kind": pn["kind"], "text": pn["text"], "figure": pn["figure"], "_source": src}
                    if pn.get("panel"):
                        ev_ent["panel"] = pn["panel"]
                    if p.get("doi"):
                        ev_ent["doi"] = p["doi"]
                    b.node(ent, "entity", ev_ent)
                b.edge("observationBase", evd, ent)

    b.resolve_nodes()     # hand edits to evidence (e.g. status, direction, class) drive the edges below

    for slug, n in list(b.nodes.items()):
        if n["kind"] != "evidence":
            continue
        ext = n["value"]["meccogFinding"]
        if ext["audit"]["status"] != "kept":
            continue
        c, d = ext["directnessClass"], ext["directnessStrength"]
        mapping = (f" Perturbed {ext['perturbed']['variable']} ({ext['perturbed']['mapsTo']}); measured "
                   f"{ext['measured']['variable']} ({ext['measured']['mapsTo']}).") if "perturbed" in ext else ""
        warrant = f"Directness class {c} ({CLASS_WORD[c]}), strength d={d} ({D_WORD[d]}).{mapping}"
        kinds = {"supports": ["supports"], "contradicts": ["opposes"], "no_effect": ["opposes"],
                 "mixed": ["supports", "opposes"]}[ext["direction"]]
        for k in kinds:
            b.edge(k, slug, "hyp", strength=STRENGTH[d], warrant=warrant, description=ext["summary"],
                   provenance={"wasGeneratedBy": "aiAssistedExtraction"})

    out = b.write(f"MecCog {hyp['id']}: {hyp['short']} — findings",
                  f"Findings extracted for MecCog Step 1, hypothesis {hyp['id']}, with sources, figure panels, "
                  f"and supports/opposes relations to the hypothesis.")
    write_readable(run_dir, out)
    n_ev = sum(1 for r in out.values() if r["kind"] == "evidence")
    print(f"mira: {len(out)} records ({n_ev} evidence) -> {b.dir}"
          + (f"; kept hand edits to {len(b.edited)}: {', '.join(b.edited[:5])}" if b.edited else ""))
    return b.dir


def write_readable(run_dir: Path, out: dict):
    claim = next(r for r in out.values() if r["kind"] == "claim")
    lines = [f"# {claim['value']['text']}", ""]
    by_src: dict[str, list] = {}
    for slug, r in out.items():
        if r["kind"] == "evidence":
            by_src.setdefault(slug.split(":")[1], []).append(r)
    rels = {}
    for r in out.values():
        if r["kind"] in ("supports", "opposes"):
            rels.setdefault(r["value"]["subject"]["uri"], []).append((r["kind"], r["value"]["strength"]))
    for pkey, evs in by_src.items():
        src = out[f"src:{pkey}"]["value"]
        lines += [f"## {src['text'][:200]}", ""]
        for r in evs:
            v = r["value"]
            rel = ", ".join(f"{k} ({s})" for k, s in rels.get(r["uri"], [])) or "no relation (audit-dropped)"
            ext = v["meccogFinding"]
            lines.append(f"- **{rel}** · {ext['dataLocation']} · class {ext['directnessClass']}, "
                         f"d={ext['directnessStrength']} — {v['text']}")
            if v["provenance"].get("excerpt"):
                lines.append(f"  > {v['provenance']['excerpt']}")
        lines.append("")
    (run_dir / "mira" / "records.md").write_text("\n".join(lines))


# ---------- read back for the sheet ---------------------------------------------------------------

def load_for_sheet(run_dir: Path) -> list[tuple[dict, list[dict]]]:
    """[(paper fields, [finding fields])] from the records, in the shape assemble.py writes. MIRA-native
    fields win where they exist (text, description, excerpt); the extension supplies the rest."""
    rec_dir = run_dir / "mira" / "records"
    by_uri = {}
    for p in rec_dir.rglob("*.json"):
        v = json.loads(p.read_text())
        nsid = p.parent.name
        by_uri[f"at://{DID}/{nsid}/{p.stem}"] = v
    src_of = {v["subject"]["uri"]: v["object"]["uri"] for v in by_uri.values()
              if v["$type"] == f"{NS}.sourceDocument"}
    papers: dict[str, tuple[dict, list]] = {}
    for uri, v in by_uri.items():
        if v["$type"] != f"{NS}.evidence" or "meccogFinding" not in v:
            continue
        ext = v["meccogFinding"]
        if ext["audit"]["status"] != "kept":
            continue
        s = by_uri[src_of[uri]]
        ms = s.get("meccogSource", {})
        paper = {"doi": s.get("doi"), "pmid": None if ms.get("pmid") in (None, "N/A") else ms["pmid"],
                 "source_type": ms.get("sourceType", "Other")}
        finding = {"description": v["text"], "quote": v.get("provenance", {}).get("excerpt", "N/A"),
                   "summary": ext["summary"], "relevance": float(ext["relevance"]),
                   "experimental_system": v.get("description", "N/A"), "data_location": ext["dataLocation"],
                   "effect_size": ext["effectSize"], "effect_size_provenance": ext["effectSizeProvenance"],
                   "p_value": ext["pValue"], "p_value_provenance": ext["pValueProvenance"],
                   "sample_size": ext["sampleSize"], "sample_size_provenance": ext["sampleSizeProvenance"],
                   "directness_class": ext["directnessClass"], "directness_strength": ext["directnessStrength"],
                   "order": ext.get("extractionOrder", 0)}
        papers.setdefault(src_of[uri], (paper, []))[1].append(finding)
    return list(papers.values())
