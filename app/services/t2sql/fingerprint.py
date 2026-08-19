"""Stable validation fingerprint for generated SQL."""

from __future__ import annotations

import hashlib


def sql_fingerprint(sql: str | None) -> str | None:
    text = " ".join(str(sql or "").split())
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
