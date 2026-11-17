# Record drafts

Each `H*/mira/` holds a hypothesis run as MIRA records: [RRGI draft lexicons](https://github.com/jring-o/rrgi-lexicons-draft) plus the MecCog extension ([docs/MIRA-EXTENSION.md](../docs/MIRA-EXTENSION.md)). Start with `records.md`, the readable view.

**Status: drafts.**
- Every evidence record was extracted by a model (`provenance.wasGeneratedBy: aiAssistedExtraction`) and checked automatically against its source PDF.
- No person has reviewed them yet. A record a person has checked will list them in `provenance.validatedBy`.
- Findings the audit dropped are included, with `meccogFinding.audit.status: "dropped"` and no supports/opposes edge.
- The DID in record URIs is a placeholder (`did:web:example.org`). Nothing has been published to a PDS.

Corrections: edit the record, run `meccog mira <H>`, and commit. Hand edits survive rebuilds (see the extension doc).

Everything else a run produces (candidates, screening, PDFs, raw extractions, the spreadsheet) stays out of git.
