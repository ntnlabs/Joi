#!/usr/bin/env python3
"""
Smoke test for Search Rework v1 migration.

Two scenarios:
  A) FRESH INSTALL: empty DB → MemoryStore() → SCHEMA_SQL runs → v15 schema.
  B) MIGRATION:    raw sqlite3 builds a v14-shaped DB with seed rows
                   (schema_version='14') → MemoryStore() opens it →
                   _migrate_to_v15_search_rework runs → v15 schema.

Both scenarios then verify:
  - schema_version == '15'
  - all 4 main tables have NOT NULL embedding (4096 bytes each)
  - all 4 vec_<surface> tables have matching rowid+embedding entries
  - user_facts.important column is gone
  - search_knowledge / search_notes / get_summaries_as_context (the RRF
    hybrid retrieval surface) execute without raising and return the right type

Scenario B additionally verifies:
  - search_v2_migrated == '1' (only the migration sets this)
  - rowids of pre-migration rows are preserved in main and vec0 tables
  - v15_lost_pin_audit row contains the off-whitelist important=1 fact
    and does NOT contain whitelist-covered facts

Requires: bge-m3 pulled in Ollama, sqlite-vec installed.
Exit code 0 on success, non-zero on failure.

Usage:
    JOI_EMBEDDING_MODEL=bge-m3 python3 tools/smoke_search_v2.py
"""
from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "execution"))
os.environ["JOI_REQUIRE_ENCRYPTED_DB"] = "0"

from joi.memory.store import MemoryStore  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def build_v14_fixture(db_path: Path) -> dict[str, int]:
    """Build a minimal v14-shaped DB at `db_path` with one row per surface.

    Returns a dict of pre-migration rowids: {"fact_id": ..., "at_risk_fact_id": ...,
    "summary_id": ..., "chunk_id": ..., "note_id": ...}. These are checked against
    post-migration rowids to verify preservation.

    Two facts are seeded:
      - `personal/name` (important=1, ON the whitelist) → auto-pinned post-migration,
         must NOT appear in v15_lost_pin_audit.
      - `hobbies/favorite_color` (important=1, OFF the whitelist) → loses its pin,
         MUST appear in v15_lost_pin_audit.
    """
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at INTEGER
        );
        CREATE TABLE user_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'inferred',
            source_message_id TEXT,
            learned_at INTEGER NOT NULL,
            updated_at INTEGER,
            last_verified_at INTEGER,
            important INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER,
            detected_at INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(conversation_id, category, key)
        );
        CREATE TABLE context_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            summary_type TEXT NOT NULL,
            period_start INTEGER NOT NULL,
            period_end INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            key_points_json TEXT
        );
        CREATE TABLE knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            embedding BLOB,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB,
            remind_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
    """)
    # NOTE: this fixture omits the v14 FTS5 mirror tables. The migration's
    # _rebuild_tables_v15 re-runs SCHEMA_SQL at the end, which re-creates them.
    # If the production DB has FTS5 tables with row counts, they will be in
    # sync because rowids are preserved through the rebuild.

    cur = conn.execute(
        "INSERT INTO user_facts (conversation_id, category, key, value, confidence, source, learned_at, important, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        ("+1234567890", "personal", "name", "Test User", 0.9, "explicit", now_ms, 1),
    )
    fact_id = cur.lastrowid
    # Off-whitelist important fact — exercises the lost-pin audit code path.
    cur = conn.execute(
        "INSERT INTO user_facts (conversation_id, category, key, value, confidence, source, learned_at, important, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        ("+1234567890", "hobbies", "favorite_color", "blue", 0.8, "explicit", now_ms, 1),
    )
    at_risk_fact_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO context_summaries (conversation_id, summary_type, period_start, period_end, summary_text, message_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("+1234567890", "daily", now_ms - 86400000, now_ms, "Discussed coffee preferences.", 5, now_ms),
    )
    summary_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO knowledge_chunks (scope, source, title, content, chunk_index, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("", "test_doc", "Coffee", "Coffee is a brewed drink.", 0, now_ms),
    )
    chunk_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO notes (conversation_id, title, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("+1234567890", "Test note", "Body.", now_ms, now_ms),
    )
    note_id = cur.lastrowid

    conn.execute(
        "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("schema_version", "14", now_ms),
    )
    conn.commit()
    conn.close()
    return {
        "fact_id": fact_id,
        "at_risk_fact_id": at_risk_fact_id,
        "summary_id": summary_id,
        "chunk_id": chunk_id,
        "note_id": note_id,
    }


def assert_v15_invariants(store: MemoryStore, label: str) -> None:
    conn = store._connect()

    v = conn.execute("SELECT value FROM system_state WHERE key = 'schema_version'").fetchone()
    if not v or v[0] != "15":
        fail(f"[{label}] schema_version != 15 (got {v[0] if v else 'missing'})")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(user_facts)").fetchall()]
    if "important" in cols:
        fail(f"[{label}] user_facts still has legacy 'important' column")
    if "pinned_override" not in cols:
        fail(f"[{label}] user_facts missing pinned_override column")
    if "embedding" not in cols:
        fail(f"[{label}] user_facts missing embedding column")

    for tbl in ("user_facts", "context_summaries", "knowledge_chunks", "notes"):
        info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        embed_col = next((c for c in info if c[1] == "embedding"), None)
        if embed_col is None:
            fail(f"[{label}] {tbl} missing embedding column")
        if embed_col[3] != 1:
            fail(f"[{label}] {tbl}.embedding is not NOT NULL")

    for vec in ("vec_user_facts", "vec_summaries", "vec_knowledge", "vec_notes"):
        if not conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (vec,)).fetchone():
            fail(f"[{label}] missing virtual table {vec}")


def assert_seed_rows_embedded(store: MemoryStore, ids: dict[str, int], label: str) -> None:
    conn = store._connect()
    pairs = [
        ("user_facts", "vec_user_facts", ids["fact_id"]),
        ("user_facts", "vec_user_facts", ids["at_risk_fact_id"]),
        ("context_summaries", "vec_summaries", ids["summary_id"]),
        ("knowledge_chunks", "vec_knowledge", ids["chunk_id"]),
        ("notes", "vec_notes", ids["note_id"]),
    ]
    for main, vec, row_id in pairs:
        main_row = conn.execute(
            f"SELECT length(embedding) FROM {main} WHERE id = ?", (row_id,)
        ).fetchone()
        if not main_row or main_row[0] != 4096:
            fail(f"[{label}] {main} id={row_id} embedding size {main_row[0] if main_row else None} != 4096")
        vec_row = conn.execute(
            f"SELECT length(embedding) FROM {vec} WHERE rowid = ?", (row_id,)
        ).fetchone()
        if not vec_row or vec_row[0] != 4096:
            fail(f"[{label}] {vec} rowid={row_id} embedding missing or wrong size")


def assert_lost_pin_audit(store: MemoryStore, ids: dict[str, int], label: str) -> None:
    """Verify the v15_lost_pin_audit captures off-whitelist facts only."""
    import json as _json
    conn = store._connect()
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'v15_lost_pin_audit'"
    ).fetchone()
    if not row:
        fail(f"[{label}] v15_lost_pin_audit row missing — migration didn't write audit")
    try:
        audit = _json.loads(row[0])
    except Exception as exc:
        fail(f"[{label}] v15_lost_pin_audit not parseable as JSON: {exc}")
    audit_ids = {entry["id"] for entry in audit}
    if ids["at_risk_fact_id"] not in audit_ids:
        fail(f"[{label}] at-risk fact id={ids['at_risk_fact_id']} missing from audit; got {audit_ids}")
    if ids["fact_id"] in audit_ids:
        fail(f"[{label}] whitelist-covered fact id={ids['fact_id']} should NOT be in audit; got {audit_ids}")


def assert_hybrid_retrieval(store: MemoryStore, label: str) -> None:
    """Smoke-check the RRF-fused retrieval methods (vec + FTS path)."""
    knowledge = store.search_knowledge(query="coffee drink", scope="", limit=5)
    if not isinstance(knowledge, list):
        fail(f"[{label}] search_knowledge returned {type(knowledge).__name__}, expected list")

    notes = store.search_notes(conversation_id="+1234567890", query="test", limit=5)
    if not isinstance(notes, list):
        fail(f"[{label}] search_notes returned {type(notes).__name__}, expected list")

    summaries = store.get_summaries_as_context(
        query="coffee", max_tokens=200, conversation_id="+1234567890",
    )
    if not isinstance(summaries, str):
        fail(f"[{label}] get_summaries_as_context returned {type(summaries).__name__}, expected str")


def scenario_fresh(tmp: Path) -> None:
    db = tmp / "fresh.sqlite"
    store = MemoryStore(db_path=str(db))
    # Seed at least one row per surface so the hybrid retrieval calls below
    # exercise non-empty result paths (FTS hits, vec hits, RRF fusion).
    store.store_fact(
        conversation_id="+1234567890",
        category="personal",
        key="name",
        value="Test User",
        confidence=0.9,
    )
    now_ms = int(time.time() * 1000)
    store.store_summary(
        summary_type="daily",
        period_start=now_ms - 86400000,
        period_end=now_ms,
        summary_text="Discussed coffee preferences.",
        message_count=5,
        conversation_id="+1234567890",
    )
    store.store_knowledge_chunk(
        source="test_doc",
        title="Coffee",
        content="Coffee is a brewed drink.",
        chunk_index=0,
        scope="",
    )
    store.add_note(
        conversation_id="+1234567890",
        title="Test note",
        content="Body about tea and herbs.",
    )

    assert_v15_invariants(store, "fresh")
    _ = store.get_pinned_facts(conversation_id="+1234567890")
    _ = store.get_facts_as_context(query="name", max_tokens=200, conversation_id="+1234567890")
    assert_hybrid_retrieval(store, "fresh")
    print("OK: scenario A (fresh install) passed")


def scenario_migration(tmp: Path) -> None:
    db = tmp / "migrate.sqlite"
    pre_ids = build_v14_fixture(db)
    store = MemoryStore(db_path=str(db))  # triggers _migrate_to_v15_search_rework

    conn = store._connect()
    marker = conn.execute(
        "SELECT value FROM system_state WHERE key = 'search_v2_migrated'"
    ).fetchone()
    if not marker or marker[0] != "1":
        fail("[migration] search_v2_migrated marker not set")

    assert_v15_invariants(store, "migration")
    assert_seed_rows_embedded(store, pre_ids, "migration")

    # The plan's migration code does NOT translate important -> pinned_override.
    # `personal/name` is on the whitelist so it auto-pins; pinned_override stays NULL.
    pin = conn.execute(
        "SELECT pinned_override FROM user_facts WHERE id = ?", (pre_ids["fact_id"],)
    ).fetchone()
    if pin[0] is not None:
        fail(f"[migration] expected pinned_override=NULL (rely on whitelist), got {pin[0]}")

    # The off-whitelist fact must also have NULL — and must show up in the audit.
    off_pin = conn.execute(
        "SELECT pinned_override FROM user_facts WHERE id = ?", (pre_ids["at_risk_fact_id"],)
    ).fetchone()
    if off_pin[0] is not None:
        fail(f"[migration] expected at-risk fact pinned_override=NULL, got {off_pin[0]}")

    assert_lost_pin_audit(store, pre_ids, "migration")
    assert_hybrid_retrieval(store, "migration")

    print("OK: scenario B (v14 -> v15 migration) passed")


def main() -> None:
    if not os.environ.get("JOI_EMBEDDING_MODEL"):
        fail("Set JOI_EMBEDDING_MODEL=bge-m3 before running this smoke test.")

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        scenario_fresh(tmp)
        scenario_migration(tmp)

    print("OK: Search Rework v1 smoke test passed (fresh + migration)")


if __name__ == "__main__":
    main()
