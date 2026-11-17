# Method

MecCog Step 1 submission pipeline: find the papers bearing on an APOE4 endosome mechanism hypothesis, and extract their experimental findings with quotes, panel-level locations, statistics with provenance, and directness, each checked against the source before it reaches the sheet.

- **Team:** OASIS Lab, University of Maryland *(name to match the submission page)*
- **Hypothesis:** H1–H5 (one spreadsheet each)
- **Submission date:** *filled in per submission*

> Status: pipeline built 2026-11-17; no submission made yet. Sections marked *per run* are completed from the run's own logs (`runs/<H>/search_log.json`, `fulltext.json`, `audit_report.md`) when a sheet is submitted, and the commit SHA on the Method sheet pins this file and the code together.

How to run it: [`docs/PIPELINE.md`](docs/PIPELINE.md).

---

## 1. Who did the work

An agent pipeline, with a person checking its output (`both` on the Method sheet once the check below is done; `agent` until then). Claude does four jobs: it writes the search queries, screens titles and abstracts, decomposes the hypothesis into ontology-grounded variables (the *frame*), and extracts findings from each paper. Deterministic code does the rest: it retrieves papers, fetches full text, checks every quote, statistic, and ontology identifier against its source, ranks the papers, and writes and validates the sheet. Each stage writes files that the next stage reads, so any stage can be rerun or replaced by hand.

## 2. Model and version

- Query writing, screening: `claude-opus-5-5` (Anthropic API), effort `medium` / `low`
- Frame and extraction: `claude-opus-5-5`, adaptive thinking, effort `high`
- If a request is declined, it is re-run on Anthropic's server-side fallback model. The model that actually answered is recorded per call, and the Method sheet lists every model that appears in the run.

## 3. How papers were found

Two independent indexes are combined and de-duplicated on PMID and DOI:

- **Europe PMC** (covers all of PubMed plus preprints): the starting reference, every paper it cites, every paper citing it, and a set of queries.
- **OpenAlex**: the same seed expansion over its citation graph, plus the same queries. Field syntax is stripped, since OpenAlex doesn't accept it.

Queries are written by Claude from the hypothesis statement, covering:
- synonyms for each variable, the named molecules, and the cell types
- at least one query aimed at null or contradicting results

Hand-written queries can be added with `--query`. Every query, its source, and its hit count go to `search_log.json`, including the ones that returned nothing and the ones truncated by OpenAlex's keyless paging limit. *Per run: queries and counts.*

**Screening (title and abstract).** Each candidate is screened for primary experimental findings on the APOE4-vs-APOE3 → endosomal-variable link. The screen keeps findings for or against the hypothesis, in any model system, and excludes reviews. The top 60 by relevance go to extraction, and the 50-paper cap is applied to the final sheet.

## 4. How full text was obtained

These routes are tried in order, and the route used is recorded per paper in `fulltext.json`:

1. **Local PDFs** placed by a person with institutional access.
2. **The PMC Open Access subset** on its public AWS bucket, giving the article PDF plus supplementary PDFs. The bucket's retraction flag is checked too.
3. **OpenAlex open-access locations**, preferring the published version, then the accepted manuscript, then the preprint. A preprint is marked as such: the extractor is told, the audit warns, and the published version stays on the access queue.
4. **Unpaywall** (optional).
5. **The DOI target**, used only if it serves a PDF directly.

No route impersonates a browser or works around a publisher's access controls. A paper with no route is extracted from its abstract only, every statistic is marked `full_text_unavailable`, and it is listed in `runs/access_queue.csv` for people with library access (UMD, UK DRI) to fetch and rerun. *Per run: count by route, and the proportion abstract-only.*

## 5. What a human checked

Every finding passes an automatic audit before it reaches the sheet. **Errors drop the finding; warnings keep it and list it for review.**

**Errors:**
- **Quote not in the paper:** under 90% verbatim coverage against the PDF text layer.
- **Estimate markers broken:** `~` without `estimated_from_figure`, or the reverse.
- **P value estimated** from a figure.
- **Value and provenance disagree.**
- **Unparseable statistic.**
- **Class 1 with strength 4–5.**
- **Paper is retracted.**

**Warnings:**
- **Printed value not found:** a `printed_in_text` number that doesn't appear in the text layer.
- **Location not at panel level.**
- **Preprint source.**
- **Class mismatch:** the directness class disagrees with the finding's own entity mapping.
- **Ontology ID rejected:** a label mismatch at OLS or HGNC.

**Human check, per run:** a person reads every warning, plus a random sample of kept findings against the PDF at the cited panel, checking the finding, the effect size, the P value, n, and the system. *Per run: who, how many, and what was corrected.*

## 6. Anything else

- **Bridging findings to the hypothesis.** Each hypothesis is decomposed once into its A and B variables, plus its intended context and intermediates, as ontology terms:
  - genes: HGNC
  - compartments and processes: GO
  - cells: CL
  - organism: NCBITaxon
  - chemicals: CHEBI

  Every finding states what it perturbed and what it measured, mapped to A, a surrogate of A, B, a surrogate of B, or an intermediate. That mapping implies the directness class (A+B=1, A′+B=2, A+B′=3, A′+B′=4), and the audit checks it against the stated class.

  These IDs use the same schemes as the Proto-OKN knowledge graphs, so `graph.json` joins to them directly. Every ID is looked up and kept only if its label matches.
- **Direction.** Contradicting and null findings are kept, labelled `CONTRADICTING:` / `NO EFFECT:`, and marked in the structured summary.
- **Statistics are reported as the paper reports them,** never converted. Percentages and folds are written unsigned because that is the validator's format, so the direction lives in the description and summary.
- **Supplements** are read only when they come through the PMC OA subset or are hand-supplied, so `supplement_only` is under-used for closed papers.
- **Known weaknesses:**
  - The quote check uses the PDF text layer, so text inside figure images can't be verified. A figure-only result carries `N/A` as its quote.
  - Figure-based estimates (`~`) come from the model's reading of the rendered page and aren't remeasured.
  - Relevance and directness strength are model judgments.

---

## Code and prompts

- `skills/extract-findings/SKILL.md` is the extraction instructions, sent as the system prompt. Its hash is stored with every extraction. The entity-mapping addendum is in `src/meccog/entities.py`.
- `src/meccog/`:
  - `search.py`, `openalex.py`: candidates
  - `screen.py`: screening
  - `fulltext.py`: retrieval
  - `entities.py`: frame, grounding, graph export
  - `extract.py`: extraction
  - `audit.py`, `pdf.py`: checks
  - `assemble.py`: sheet and validator
  - `queue.py`: access queue
- `tests/` runs offline: audit → assemble → the official validator on a synthetic paper.

The quote audit is adapted from our discourse-graph extraction pipeline ([scientific-claim-extraction-benchmarking](https://github.com/oasisresearchlab/scientific-claim-extraction-benchmarking)). License: MIT.
