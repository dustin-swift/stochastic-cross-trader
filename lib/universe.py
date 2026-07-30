"""Post-Finviz sector diversification cap (spec §6a). The 52wk-high-proximity
and above-SMA precision filtering that used to live here is gone — the Finviz
Elite screener applies both natively as filter fields in the user's manual
export (see providers/finviz.py), so there's nothing left to compute here
except the per-sector cap. Pure function, no network calls.
"""
from __future__ import annotations

from typing import Mapping, Sequence


def cap_per_sector(candidates: Sequence[Mapping], max_per_sector: int) -> list[Mapping]:
    """Trim `candidates` to at most `max_per_sector` entries per `sector` key,
    preserving input order (so callers should pre-sort by whatever priority —
    e.g. closest to 52wk high, largest volume — matters before calling this).
    """
    counts: dict[str, int] = {}
    kept: list[Mapping] = []
    for candidate in candidates:
        sector = candidate.get("sector", "UNKNOWN")
        if counts.get(sector, 0) >= max_per_sector:
            continue
        counts[sector] = counts.get(sector, 0) + 1
        kept.append(candidate)
    return kept
