# Running the pipeline

Needs Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). Claude calls use the Anthropic SDK's usual credentials: `ANTHROPIC_API_KEY`, or a profile from `ant auth login`.

```
cp team.example.toml team.toml          # team, leader, email, method URL
uv run --python 3.12 meccog resources   # official template, validator, example (not committed)
uv run --python 3.12 meccog all H1      # or step by step:
```

| step | reads | writes | calls |
|---|---|---|---|
| `search H1` | hypothesis, seed PMID | `candidates.jsonl`, `search_log.json` | Europe PMC, OpenAlex, Claude (queries) |
| `screen H1` | candidates | `screen.jsonl` (resumable) | Claude, effort low |
| `frame H1` | hypothesis | `frame.json` (edit it, then set `"edited_by_human": true`) | Claude + OLS/HGNC |
| `fetch H1` | shortlist | `fulltext.json`, `pdfs/`, `needs_pdf.md` | PMC S3, OpenAlex, Unpaywall |
| `extract H1` | shortlist + frame | `extractions/<PMID>.json` (resumable) | Claude, effort high, PDF in |
| `audit H1` | extractions | `audit.json`, `audit_report.md`, `graph.json` | OLS/HGNC only |
| `assemble H1` | audit | `meccog_H1_submission.xlsx`, `validator.txt` | none |
| `queue` | every `screen.jsonl` | `runs/access_queue.csv`, `.md` | PMC S3, OpenAlex |

Everything lands in `runs/H1/` (gitignored). Steps are resumable, so a failure on one paper costs only that paper.

## The access queue

`meccog queue` lists papers that screened relevant (≥ 0.6) but have no open published or accepted copy. These are ranked by relevance, with papers relevant to several hypotheses ranked up.
- Paste the CSV into a shared sheet. People with library access claim rows and save PDFs into `pdfs/` under the `save_as` name (`<PMID>.pdf`, supplements as `<PMID>_supp1.pdf`).
- Then delete that paper's `runs/<H>/extractions/<PMID>.json` and rerun `extract`, `audit` and `assemble`.

## Environment

| variable | effect |
|---|---|
| `OPENALEX_API_KEY` | lifts OpenAlex's keyless limits (one page of citing works, small daily credit budget) |
| `UNPAYWALL_EMAIL` | enables the Unpaywall route (Unpaywall asks for an address you choose) |
| `MECCOG_EXTRACT_MODEL`, `MECCOG_SCREEN_MODEL` | override the model (default `claude-opus-5-5`) |

## Cost, roughly

Per hypothesis:
- **Screening:** a few hundred abstracts at low effort, a few dollars.
- **Extraction:** about 60 PDFs at high effort, on the order of $10–20.

The run records token usage per paper in `extractions/*.json`.

## Before submitting

1. Commit, so the Method sheet's SHA pins the code that ran: `assemble` warns if `src/` or `skills/` are dirty.
2. Read `audit_report.md`. Check every warning, plus a sample of kept findings against the PDF. Record what you checked in README §5, and set `who_did_the_work = "both"`.
3. Fill in the *per run* parts of README §3–5 from the run's logs.
4. The team leader uploads on the submission page.
