# MIRA output and the MecCog extension

The pipeline's canonical output is a set of MIRA records per hypothesis (`runs/<H>/mira/`). They use the shapes of the RRGI draft lexicons ([`tech.scios.rrgi.*`](https://github.com/jring-o/rrgi-lexicons-draft)), which implement the [MIRA community schema](https://github.com/MIRA-science/schema). The challenge spreadsheet is a view rendered from those records. Fixing a run means editing records, not cells.

MIRA has no place for some of what the challenge asks for, so the records carry a small **extension**, `org.oasisresearchlab.meccog.defs` ([lexicon](../lexicons/org.oasisresearchlab.meccog.defs.json)). This page says what it adds, why, and what might belong upstream.

## Mapping

| MecCog | MIRA record / field | extension |
|---|---|---|
| hypothesis | `claim.text`; A, B and context as `contextEntities` with ontology URIs | `meccogHypothesis`: id, seed PMID, A/B variable phrases |
| finding description | `evidence.text` | |
| experimental system | `evidence.description` | |
| finding quote | `evidence.provenance.excerpt`, with `anchorLocations` (page boxes, PDF points) and `anchorLocationsMode` | |
| estimate read off a figure | `evidence.plotReading: true` | the `~` value and `estimated_from_figure` code |
| perturbed / measured / system terms | `evidence.contextEntities` (`entityType` "perturbed (A)", "measured (B_surrogate)", "experimental system"; `uri` an OBO PURL or identifiers.org HGNC) | `perturbed` / `measured`: variable phrase and its mapping to A, A′, B, B′ |
| data location | `entity` records (`kind` figurePanel / table, figure, panel) linked by `observationBase` | `dataLocation`, the string as the sheet prints it |
| effect size, P value, n, and their provenance codes | none | `meccogFinding.*` |
| directness class and strength d | `supports` / `opposes` edge: `strength` (d 1–2 strong, 3 moderate, 4–5 weak), `warrant` states the class and the mapping | `directnessClass`, `directnessStrength` (the exact values) |
| for / against / no effect | `supports` or `opposes` (no effect → opposes; mixed → both) | `direction` |
| relevance, structured summary | none | `relevance` (decimal string), `summary` |
| audit result | `tags` include `audit-dropped` | `audit`: status, errors, warnings, quote coverage |
| paper | `source` (`text`, `doi`, `url`, `sourceType` paper/preprint), linked by `sourceDocument` | `meccogSource`: PMID, MecCog source type, full-text route and version, retraction flag |
| the pipeline | `protocol` with the commit; everything in one `bundle` | |

**Rule:** where a base field exists, it is the source of truth, and the extension never repeats it. The sheet reads `evidence.text`, `evidence.description` and `provenance.excerpt` from the base record, and everything else from the extension.

## Why an extension, and not text in `description`

- **Statistics with provenance are the challenge's hardest part, and its assessment checks them one by one.** Free text would have to be parsed back out for the sheet, and parsing is where errors creep in.
- **The provenance codes encode a distinction MIRA doesn't make:** a value the paper never reported (`not_found_in_source`) versus one we couldn't reach (`full_text_unavailable`). That distinction is one of the things the challenge measures.
- **Directness is two numbers with defined semantics.** `relationStrength` is a projection of d, not a replacement: four strength words can't round-trip five classes times five strengths.

## Encoding notes

- Each extension object is embedded on its base record under its own key (`meccogFinding`, `meccogSource`, `meccogHypothesis`), with a `$type` naming its def, as atproto does for typed embedded objects. A reader that doesn't know the extension ignores it.
- Decimals are strings (`"0.85"`), because DAG-CBOR has no floats.
- Record keys are TIDs derived from the run's fixed timestamp and a hash of a stable slug, so rebuilding never renames a record. CIDs are CIDv1 / dag-cbor / sha-256, and relations pin both endpoints by CID.
- The DID is a placeholder (`did:web:example.org`) until the records are published to a PDS.

## Editing a run

1. Edit any record under `runs/<H>/mira/records/`. For example, correct an effect size, change `directnessStrength`, reword `evidence.text`, or set `meccogFinding.audit.status` to `"kept"` / `"dropped"`.
2. Run `meccog mira <H>`. Edited records are detected by fingerprint and kept, then marked `edited` in `index.json` so later rebuilds keep them too. Their CIDs are recomputed, and every relation pointing at them is re-pinned. The supports/opposes edges are re-derived from the edited direction, status and directness.
3. Run `meccog assemble <H>`. The sheet is rendered from the records and validated.

To discard an edit, delete the record's file and rebuild.

## Candidates for upstream

These would be worth proposing to RRGI/MIRA, since they aren't MecCog-specific:

- **Reported statistics on evidence:** effect size, test statistic, P value, n. Each would carry a provenance code distinguishing printed, read-off-figure, estimated, supplement-only, not-reported and not-accessed. `plotReading` already covers one corner of this.
- **Directness of evidence relative to a claim:** surrogate input and output, the A′/B′ scheme. This could be a structured warrant on `supports` / `opposes`, more precise than `relationStrength`.
- **Variable mapping on evidence:** which context entity was perturbed and which measured, and how each maps to the claim's variables. This is what links a finding to a mechanism hypothesis.
