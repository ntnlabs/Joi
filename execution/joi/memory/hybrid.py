"""
Hybrid retrieval helpers: RRF fusion of vector and FTS search results.

Pipeline (caller-side, in store.py):
    1. Pre-filter: SQL on main table → set of eligible rowids
    2. Two retrievers, each top-N within the eligible set:
        - vec_search:  sqlite-vec KNN on vec_<surface>
        - fts_search:  FTS5 BM25 on <surface>_fts
    3. rrf_fuse: combine the two ranked lists into one
    4. Caller takes top-K, hydrates rows, formats for prompt.

This module is pure algorithm + SQL helpers. No connection management,
no embedding (that's _get_embedding in store.py).
"""

from __future__ import annotations
import logging
import os
import sqlite3
from typing import Sequence


logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back to default.

    Logs and returns the default on any malformed input — fail-soft for
    operator typos (the constants have safe defaults).
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
        if v <= 0:
            raise ValueError("must be positive")
        return v
    except ValueError as e:
        logger.warning(
            "Invalid env override; using default",
            extra={"env": name, "raw": raw, "default": default, "error": str(e)},
        )
        return default


# Industry-standard RRF smoothing constant (Cormack et al. 2009).
# High k => consensus across retrievers dominates over absolute rank order.
# Operator override: JOI_RRF_K
RRF_K_DEFAULT: int = _env_int("JOI_RRF_K", 60)

# How many results to pull from each retriever before fusion.
# 2× the typical caller's K provides enough overlap room.
# Operator override: JOI_RRF_TOP_N
RRF_TOP_N_DEFAULT: int = _env_int("JOI_RRF_TOP_N", 20)


def rrf_fuse(
    ranked_lists: Sequence[Sequence[int]],
    k: int = RRF_K_DEFAULT,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of multiple ranked lists of rowids.

    Args:
        ranked_lists: Each inner sequence is a ranked list of rowids
                      (rank 0 = best). Different lists may overlap.
        k: Smoothing constant (default 60).

    Returns:
        List of (rowid, score) sorted by score DESC. A rowid that
        appears in N lists gets N terms summed.

    Example:
        vec_hits = [10, 20, 30]   # rowid 10 ranked #1 by vector
        fts_hits = [20, 10, 40]   # rowid 20 ranked #1 by FTS
        rrf_fuse([vec_hits, fts_hits])
        # rowid 10: 1/(60+0) + 1/(60+1) = 0.0331
        # rowid 20: 1/(60+1) + 1/(60+0) = 0.0331
        # rowid 30: 1/(60+2)            = 0.0161
        # rowid 40: 1/(60+2)            = 0.0161
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, rowid in enumerate(ranked):
            scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def eligible_rowids(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence,
) -> list[int]:
    """Run a pre-filter SQL that selects rowids of eligible rows.

    The SQL must SELECT a single integer column (the rowid / id).
    Multi-user isolation (conversation_id), TTL, soft-deletes, and
    pinning exclusions all live in this SQL — there is exactly one
    place where they are enforced.
    """
    cursor = conn.execute(sql, params)
    return [row[0] for row in cursor.fetchall()]


def vec_search(
    conn: sqlite3.Connection,
    vec_table: str,
    query_vec_bytes: bytes,
    eligible: list[int],
    top_n: int = RRF_TOP_N_DEFAULT,
) -> list[int]:
    """KNN over a sqlite-vec virtual table, restricted to `eligible` rowids.

    Returns up to `top_n` rowids in similarity order (closest first).
    Returns [] if `eligible` is empty.
    """
    if not eligible:
        return []
    placeholders = ",".join("?" for _ in eligible)
    sql = f"""
        SELECT rowid
        FROM {vec_table}
        WHERE embedding MATCH ?
          AND rowid IN ({placeholders})
        ORDER BY distance
        LIMIT ?
    """
    cursor = conn.execute(sql, [query_vec_bytes, *eligible, top_n])
    return [row[0] for row in cursor.fetchall()]


def fts_search(
    conn: sqlite3.Connection,
    fts_table: str,
    fts_query: str,
    eligible: list[int],
    top_n: int = RRF_TOP_N_DEFAULT,
) -> list[int]:
    """BM25 search over an FTS5 virtual table, restricted to `eligible` rowids.

    `fts_query` must already be sanitized by the caller (use the existing
    word-extraction + OR-join pattern from search_facts).

    Returns up to `top_n` rowids in BM25 rank order (best first).
    Returns [] if `eligible` is empty or `fts_query` is empty.
    """
    if not eligible or not fts_query:
        return []
    placeholders = ",".join("?" for _ in eligible)
    sql = f"""
        SELECT rowid
        FROM {fts_table}
        WHERE {fts_table} MATCH ?
          AND rowid IN ({placeholders})
        ORDER BY rank
        LIMIT ?
    """
    try:
        cursor = conn.execute(sql, [fts_query, *eligible, top_n])
        return [row[0] for row in cursor.fetchall()]
    except sqlite3.OperationalError as e:
        logger.warning("FTS search failed", extra={"table": fts_table, "error": str(e)})
        return []
