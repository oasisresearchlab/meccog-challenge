"""Verbatim matching against PDF text.

Ported from dgpipe (scientific-claim-extraction-benchmarking/pipeline/src/dgpipe/pdf.py), the audit our
language-and-health extraction pipeline uses: normalize both sides to lowercase alphanumerics, then measure
how much of the quote appears, in order, in the source.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf


def norm(s: str) -> str:
    """NFKD + lowercase alphanumerics only: punctuation, spacing, ligatures and case may differ."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s).lower())


def coverage(quote: str, source_norm: str) -> float:
    """Fraction of the normalized quote matched, in order, in the normalized source text."""
    q = norm(quote)
    if not q or q in source_norm:
        return 1.0
    sm = SequenceMatcher(None, q, source_norm, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(q)


def best_coverage(quote: str, pages_norm: list[str], hint_page: int | None) -> tuple[float, int | None]:
    """Coverage on the hinted page and its neighbours first (cheap, and quotes break across pages),
    then on every page; returns (coverage, 1-based page)."""
    q = norm(quote)
    if not q:
        return 0.0, None
    for i, p in enumerate(pages_norm, 1):
        if q in p:
            return 1.0, i
    order = []
    if hint_page:
        order += [p for p in (hint_page, hint_page - 1, hint_page + 1) if 1 <= p <= len(pages_norm)]
    order += [p for p in range(1, len(pages_norm) + 1) if p not in order]
    best, where = 0.0, None
    for p in order:
        window = "".join(pages_norm[max(0, p - 1):p + 1])  # page + next, for quotes that straddle
        c = coverage(quote, window)
        if c > best:
            best, where = c, p
        if best >= 0.98:
            break
    return best, where


def tier(c: float) -> str:
    return "ok" if c >= 0.98 else "minor" if c >= 0.90 else "moderate" if c >= 0.50 else "fail"


def pages(pdf: Path) -> list[str]:
    with pymupdf.open(pdf) as doc:
        return [p.get_text() for p in doc]
