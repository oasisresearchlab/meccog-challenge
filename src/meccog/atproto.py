"""Content addressing for AT Protocol records: deterministic DAG-CBOR, CIDv1, and TID record keys.

Ported from the MIRA writer in scientific-claim-extraction-benchmarking
(studies/language-and-health/src/langhealth/mira.py). The CID is CIDv1 / dag-cbor / sha-256 of the record,
so a relation's strongRef pins the exact version of each endpoint. DAG-CBOR in atproto has no floats:
decimals go in records as strings.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime


def _cbor_head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for extra, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * size):
            return bytes([major << 5 | extra]) + n.to_bytes(size, "big")
    raise ValueError(n)


def dag_cbor(o) -> bytes:
    if o is None:
        return b"\xf6"
    if o is True or o is False:
        return b"\xf5" if o else b"\xf4"
    if isinstance(o, int):
        return _cbor_head(0, o) if o >= 0 else _cbor_head(1, -1 - o)
    if isinstance(o, str):
        b = o.encode()
        return _cbor_head(3, len(b)) + b
    if isinstance(o, list):
        return _cbor_head(4, len(o)) + b"".join(dag_cbor(x) for x in o)
    if isinstance(o, dict):
        keys = sorted(o, key=lambda k: (len(k.encode()), k.encode()))
        return _cbor_head(5, len(o)) + b"".join(dag_cbor(k) + dag_cbor(o[k]) for k in keys)
    raise TypeError(f"not encodable in an atproto record (floats are not allowed): {type(o).__name__} {o!r}")


def cid(record: dict) -> str:
    raw = bytes([0x01, 0x71, 0x12, 0x20]) + hashlib.sha256(dag_cbor(record)).digest()
    return "b" + base64.b32encode(raw).decode().lower().rstrip("=")


_B32_SORTABLE = "234567abcdefghijklmnopqrstuvwxyz"


def tid(created_at: str, slug: str) -> str:
    """A TID-form record key, stable per slug: the run's fixed timestamp plus a hash of the slug, so adding or
    removing other records never renames this one."""
    micros = int(datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp() * 1_000_000)
    seq = int(hashlib.sha256(slug.encode()).hexdigest()[:5], 16)      # < 2^20
    n = (micros + seq) << 10
    return "".join(_B32_SORTABLE[(n >> (5 * i)) & 31] for i in reversed(range(13)))
