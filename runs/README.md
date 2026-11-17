# Record drafts

Each `H*/mira/` holds a hypothesis run as MIRA records: [RRGI draft lexicons](https://github.com/jring-o/rrgi-lexicons-draft) plus the MecCog extension ([docs/MIRA-EXTENSION.md](../docs/MIRA-EXTENSION.md)). Start with `records.md`, the readable view.

**Status: drafts.**
- Every evidence record was extracted by a model (`provenance.wasGeneratedBy: aiAssistedExtraction`) and checked automatically against its source PDF.
- No person has reviewed them yet. A record a person has checked will list them in `provenance.validatedBy`.
- Findings the audit dropped are included, with `meccogFinding.audit.status: "dropped"` and no supports/opposes edge.
- The DID in record URIs is a placeholder (`did:web:example.org`). Nothing has been published to a PDS.

Corrections: edit the record, run `meccog mira <H>`, and commit. Hand edits survive rebuilds (see the extension doc).

## Full-text manifest

`H*/fulltext.json` records, for each paper:
- which file was read and how it was obtained: `local`, `pmc_s3`, `openalex`, `unpaywall`, `doi`, or `abstract_only`
- its license and version
- the sha256 and size of the PDF and each supplement

The PDFs themselves are not in git (one hypothesis is about 400 MB, and hand-supplied copies may be paywalled):
- **Open-access copies:** re-fetch with `meccog fetch <H>` and check them against the hashes.
- **Hand-supplied copies** (`route: local`): named and hashed only.

Everything else a run produces (candidates, screening, PDFs, raw extractions, the spreadsheet) stays out of git.
