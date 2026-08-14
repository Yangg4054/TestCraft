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

CREATE TABLE IF NOT EXISTS case_sets (
    set_id      TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    description TEXT        NOT NULL DEFAULT '',
    cases       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    case_count  INTEGER     NOT NULL DEFAULT 0,
    created_at  TEXT        NOT NULL DEFAULT '',
    updated_at  TEXT        NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_case_sets_updated_at ON case_sets (updated_at DESC);

CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS requirements (
    id             TEXT PRIMARY KEY,
    title          TEXT  NOT NULL DEFAULT '',
    content        TEXT  NOT NULL DEFAULT '',
    priority       TEXT  NOT NULL DEFAULT 'P1',
    status         TEXT  NOT NULL DEFAULT '待分析',
    source         TEXT  NOT NULL DEFAULT '',
    source_url     TEXT  NOT NULL DEFAULT '',
    feature_points JSONB NOT NULL DEFAULT '[]'::jsonb,
    extra          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TEXT  NOT NULL DEFAULT '',
    updated_at     TEXT  NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_requirements_updated_at ON requirements (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_requirements_status ON requirements (status);
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


# --- 用例集（case_sets）---------------------------------------------------
# 记录形状与 case_sets.py 的文件后端保持一致，两种后端可无缝互换。

def save_case_set(record: dict) -> None:
    """插入或整体覆盖一个用例集。"""
    cases = record.get("cases") or []
    with _get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO case_sets (
                set_id, name, description, cases, case_count, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (set_id) DO UPDATE SET
                name        = EXCLUDED.name,
                description = EXCLUDED.description,
                cases       = EXCLUDED.cases,
                case_count  = EXCLUDED.case_count,
                updated_at  = EXCLUDED.updated_at
            """,
            (
                record.get("id"),
                record.get("name", ""),
                record.get("description", ""),
                Jsonb(cases),
                len(cases),
                record.get("created_at", ""),
                record.get("updated_at", ""),
            ),
        )


def get_case_set(set_id: str) -> dict | None:
    with _get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT set_id, name, description, cases, created_at, updated_at
            FROM case_sets WHERE set_id = %s
            """,
            (set_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "cases": row[3] or [],
        "created_at": row[4],
        "updated_at": row[5],
    }


def list_case_sets(limit: int = 200) -> list[dict]:
    with _get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT set_id, name, description, case_count, created_at, updated_at
            FROM case_sets ORDER BY updated_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "case_count": r[3],
            "created_at": r[4],
            "updated_at": r[5],
        }
        for r in rows
    ]


def delete_case_set(set_id: str) -> bool:
    with _get_pool().connection() as conn:
        result = conn.execute("DELETE FROM case_sets WHERE set_id = %s", (set_id,))
    return bool(result.rowcount)


# --- 需求池（requirements）------------------------------------------------
# 需求 + 嵌套的功能点原来只写在 Pod 本地的 requirements.json，重启 / 多副本会丢。
# 结构化字段独立成列便于查询与排序，feature_points 整块存 JSONB（和 test_cases 一致）。
#
# 两个设计取舍：
# - created_at / updated_at 用 TEXT 而不是 TIMESTAMPTZ：应用层全程按
#   "%Y-%m-%d %H:%M:%S" 字符串比较和切片渲染，用 TEXT 才能和文件后端行为完全一致。
# - 额外加一列 extra：需求记录上可能挂着列以外的键（如上传入池时回写的
#   test_case_run_id / test_case_count），整体存进 extra 才不会在往返中被悄悄丢掉。

_REQUIREMENT_COLUMNS = (
    "id", "title", "content", "priority", "status",
    "source", "source_url", "created_at", "updated_at",
)


def _requirement_to_row(requirement: dict) -> tuple:
    """Split a requirement dict into fixed columns + feature_points + extra."""
    extra = {
        key: value for key, value in requirement.items()
        if key not in _REQUIREMENT_COLUMNS and key != "feature_points"
    }
    feature_points = requirement.get("feature_points") or []
    return (
        str(requirement.get("id", "")),
        str(requirement.get("title", "") or ""),
        str(requirement.get("content", "") or ""),
        str(requirement.get("priority", "") or "P1"),
        str(requirement.get("status", "") or "待分析"),
        str(requirement.get("source", "") or ""),
        str(requirement.get("source_url", "") or ""),
        Jsonb(feature_points),
        Jsonb(extra),
        str(requirement.get("created_at", "") or ""),
        str(requirement.get("updated_at", "") or ""),
    )


def _row_to_requirement(row) -> dict:
    """Rebuild the exact dict shape the application (and templates) expect."""
    requirement = {
        "id": row[0],
        "title": row[1],
        "content": row[2],
        "priority": row[3],
        "status": row[4],
        "source": row[5],
        "source_url": row[6],
        "feature_points": row[7] or [],
        "created_at": row[9],
        "updated_at": row[10],
    }
    # extra 里的键回填到顶层，保证 PG 往返后字典和存进去时一致。
    if isinstance(row[8], dict):
        for key, value in row[8].items():
            requirement.setdefault(key, value)
    return requirement


_REQUIREMENT_SELECT = """
    SELECT id, title, content, priority, status, source, source_url,
           feature_points, extra, created_at, updated_at
    FROM requirements
"""

_REQUIREMENT_UPSERT = """
    INSERT INTO requirements (
        id, title, content, priority, status, source, source_url,
        feature_points, extra, created_at, updated_at
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
        title          = EXCLUDED.title,
        content        = EXCLUDED.content,
        priority       = EXCLUDED.priority,
        status         = EXCLUDED.status,
        source         = EXCLUDED.source,
        source_url     = EXCLUDED.source_url,
        feature_points = EXCLUDED.feature_points,
        extra          = EXCLUDED.extra,
        created_at     = EXCLUDED.created_at,
        updated_at     = EXCLUDED.updated_at
"""


def save_requirement(requirement: dict) -> None:
    """Insert or overwrite one requirement (including its feature points)."""
    if not requirement.get("id"):
        raise ValueError("需求缺少 id，无法保存。")
    with _get_pool().connection() as conn:
        conn.execute(_REQUIREMENT_UPSERT, _requirement_to_row(requirement))


def get_requirement(requirement_id: str) -> dict | None:
    if not requirement_id:
        return None
    with _get_pool().connection() as conn:
        row = conn.execute(
            _REQUIREMENT_SELECT + " WHERE id = %s", (requirement_id,)
        ).fetchone()
    return _row_to_requirement(row) if row else None


def list_requirements(limit: int = 500) -> list[dict]:
    """Newest first, matching the ordering the requirement pool renders."""
    with _get_pool().connection() as conn:
        rows = conn.execute(
            _REQUIREMENT_SELECT + " ORDER BY updated_at DESC, id LIMIT %s", (limit,)
        ).fetchall()
    return [_row_to_requirement(row) for row in rows]


def delete_requirement(requirement_id: str) -> bool:
    with _get_pool().connection() as conn:
        result = conn.execute("DELETE FROM requirements WHERE id = %s", (requirement_id,))
    return bool(result.rowcount)


def sync_requirements(requirements: list[dict]) -> None:
    """Replace the whole pool with `requirements`.

    应用层的 _save_requirements 一直是"整表覆盖写"语义（删除需求就是把它从列表里
    去掉再整体写回），所以这里必须 upsert + 删除不在列表中的行，只做 upsert 会漏删。
    两步放在同一个事务里，避免中途失败留下半个状态。
    """
    rows = [_requirement_to_row(item) for item in requirements if item.get("id")]
    keep_ids = [row[0] for row in rows]
    with _get_pool().connection() as conn:
        with conn.transaction():
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(_REQUIREMENT_UPSERT, rows)
            # keep_ids 为空数组时 id = ANY('{}') 恒假，等价于清空整张表，
            # 与写入空列表的文件行为一致。
            conn.execute(
                "DELETE FROM requirements WHERE NOT (id = ANY(%s))", (keep_ids,)
            )


# --- 应用配置（app_config）------------------------------------------------
# 大模型与飞书凭证以前只写在容器内的 config.json，重启即丢。存进数据库后，
# 配置随实例重建、扩缩容和滚动发布保留。

def load_app_config() -> dict[str, str]:
    """Return every stored config key. Empty dict when nothing was saved yet."""
    with _get_pool().connection() as conn:
        rows = conn.execute("SELECT key, value FROM app_config").fetchall()
    return {row[0]: row[1] for row in rows}


def save_app_config(values: dict, updated_at: str = "") -> None:
    """Upsert config keys. Only the keys present in `values` are touched."""
    if not values:
        return
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO app_config (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    value      = EXCLUDED.value,
                    updated_at = EXCLUDED.updated_at
                """,
                [(key, str(value or ""), updated_at) for key, value in values.items()],
            )
