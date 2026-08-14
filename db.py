"""PostgreSQL persistence for TestCraft.

Stores each generation run — the parsed requirement document plus the generated
test-case set (as JSONB) — so history survives pod restarts and the app can run
with multiple replicas (no more local-disk state).

Connection is configured purely via environment variables so the same image runs
locally and in k8s (where the URL is injected from a KMS-backed Secret):

    TESTCRAFT_DATABASE_URL   preferred, e.g. postgresql://user:pass@host:5432/testcraft
    DATABASE_URL             fallback name

When neither is set the store is disabled (is_enabled() -> False) and the caller
falls back to the legacy local-file behaviour, so `python app.py` still works
without a database for quick local trials.
"""

import logging
import os

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_DATABASE_URL = (
    os.environ.get("TESTCRAFT_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or ""
).strip()

_pool: ConnectionPool | None = None


def is_enabled() -> bool:
    """True when a database URL is configured."""
    return bool(_DATABASE_URL)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not _DATABASE_URL:
            raise RuntimeError(
                "数据库未配置：请设置 TESTCRAFT_DATABASE_URL 或 DATABASE_URL 环境变量"
            )
        # Small pool — this is a low-QPS internal tool. open=True so a bad URL
        # fails fast at startup rather than on the first request.
        _pool = ConnectionPool(
            _DATABASE_URL,
            min_size=1,
            max_size=5,
            max_idle=300,
            kwargs={"autocommit": True},
            open=True,
        )
    return _pool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS test_runs (
    run_id              TEXT PRIMARY KEY,
    requirement_text    TEXT,
    requirement_source  TEXT,
    code_structure_text TEXT,
    test_cases          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    case_count          INTEGER     NOT NULL DEFAULT 0,
    feishu_doc_url      TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_test_runs_created_at ON test_runs (created_at DESC);
"""


def init_db() -> None:
    """Create the schema if it does not exist. Safe to call on every boot."""
    with _get_pool().connection() as conn:
        conn.execute(_SCHEMA)
    logger.info("test_runs schema ensured")


def save_run(
    run_id: str,
    test_cases: list[dict],
    *,
    requirement_text: str | None = None,
    requirement_source: str | None = None,
    code_structure_text: str | None = None,
    feishu_doc_url: str | None = None,
) -> None:
    """Insert (or overwrite) a run. Upsert keeps /api/regenerate idempotent."""
    with _get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO test_runs (
                run_id, requirement_text, requirement_source,
                code_structure_text, test_cases, case_count, feishu_doc_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                requirement_text    = EXCLUDED.requirement_text,
                requirement_source  = EXCLUDED.requirement_source,
                code_structure_text = EXCLUDED.code_structure_text,
                test_cases          = EXCLUDED.test_cases,
                case_count          = EXCLUDED.case_count,
                feishu_doc_url       = EXCLUDED.feishu_doc_url
            """,
            (
                run_id,
                requirement_text,
                requirement_source,
                code_structure_text,
                Jsonb(test_cases),
                len(test_cases),
                feishu_doc_url,
            ),
        )


def get_run(run_id: str) -> dict | None:
    """Fetch a single run, or None. test_cases comes back as a Python list."""
    with _get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT run_id, requirement_text, requirement_source, code_structure_text,
                   test_cases, case_count, feishu_doc_url, created_at
            FROM test_runs WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "run_id": row[0],
        "requirement_text": row[1],
        "requirement_source": row[2],
        "code_structure_text": row[3],
        "test_cases": row[4],
        "count": row[5],
        "feishu_doc_url": row[6] or "",
        "created_at": row[7],
    }


def list_runs(limit: int = 200) -> list[dict]:
    """History listing: newest first. Shape matches history.html (run_id/count/time)."""
    with _get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT run_id, case_count, created_at
            FROM test_runs ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "run_id": r[0],
            "count": r[1],
            "time": r[2].strftime("%Y-%m-%d %H:%M"),
        }
        for r in rows
    ]


def update_feishu_url(run_id: str, url: str) -> None:
    with _get_pool().connection() as conn:
        conn.execute(
            "UPDATE test_runs SET feishu_doc_url = %s WHERE run_id = %s",
            (url, run_id),
        )
