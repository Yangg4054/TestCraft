"""用例集（Case Set）持久化层。

用例集是测试用例的长期资产：一次生成的运行结果（run）是临时产物，
用户从结果页勾选满意的用例保存进用例集，用例集支持增删改查和导出。

与 db.py 保持同样的双后端策略：
- 配置了数据库时走 PostgreSQL（多副本、重启不丢）
- 未配置时落到本地 JSON 文件，`python app.py` 开箱可用
"""

import json
import logging
import os
import tempfile
import time
import uuid

import db

logger = logging.getLogger(__name__)

# 本地文件后端的存储位置。测试可直接改写本模块属性。
STORE_FILE = os.environ.get(
    "TESTCRAFT_CASE_SETS_FILE",
    os.path.join(
        os.environ.get("TESTCRAFT_OUTPUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")),
        "case_sets.json",
    ),
)

CASE_FIELDS = (
    "id", "module", "name", "priority", "preconditions",
    "steps", "expected_result", "type", "method",
)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _use_db() -> bool:
    return db.is_enabled()


# --- 本地文件后端 ---------------------------------------------------------

def _read_file_store() -> list[dict]:
    if not os.path.exists(STORE_FILE):
        return []
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        logger.exception("读取用例集文件失败：%s", STORE_FILE)
        return []


def _write_file_store(sets: list[dict]) -> None:
    directory = os.path.dirname(STORE_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    # 同目录临时文件 + 原子替换，避免写入中断损坏存储。
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".case_sets_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sets, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, STORE_FILE)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


# --- 用例规范化 -----------------------------------------------------------

def normalize_case(case: dict, **provenance) -> dict:
    """把任意来源的用例裁剪成用例集统一字段，并附带来源信息。"""
    normalized = {field: str(case.get(field, "") or "").strip() for field in CASE_FIELDS}
    if normalized["priority"].upper() in {"P0", "P1", "P2", "P3"}:
        normalized["priority"] = normalized["priority"].upper()
    else:
        normalized["priority"] = "P2"
    normalized["module"] = normalized["module"] or "General"
    normalized["type"] = normalized["type"] or "Functional"
    normalized.update({
        "source_run_id": str(provenance.get("source_run_id", "") or ""),
        "source_requirement_id": str(provenance.get("source_requirement_id", "") or ""),
        "source_requirement_title": str(provenance.get("source_requirement_title", "") or ""),
        "source_feature_id": str(provenance.get("source_feature_id", "") or ""),
        "added_at": provenance.get("added_at") or _now(),
    })
    return normalized


def _dedupe_key(case: dict) -> tuple[str, str]:
    return (
        str(case.get("module", "")).strip().casefold(),
        str(case.get("name", "")).strip().casefold(),
    )


def _renumber(cases: list[dict]) -> list[dict]:
    for index, case in enumerate(cases, 1):
        case["id"] = f"TC-{index:03d}"
    return cases


def _summary(record: dict) -> dict:
    """列表页需要的轻量视图，不带 cases 正文。"""
    return {
        "id": record.get("id", ""),
        "name": record.get("name", ""),
        "description": record.get("description", ""),
        "case_count": len(record.get("cases", []) or []),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
    }


# --- 对外 API -------------------------------------------------------------

def list_sets() -> list[dict]:
    """按更新时间倒序返回全部用例集摘要。"""
    if _use_db():
        return db.list_case_sets()
    records = _read_file_store()
    summaries = [_summary(record) for record in records]
    summaries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return summaries


def get_set(set_id: str) -> dict | None:
    if not set_id:
        return None
    if _use_db():
        return db.get_case_set(set_id)
    for record in _read_file_store():
        if record.get("id") == set_id:
            return record
    return None


def create_set(name: str, description: str = "", cases: list[dict] | None = None) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("用例集名称不能为空。")
    now = _now()
    record = {
        "id": uuid.uuid4().hex[:12],
        "name": name[:120],
        "description": str(description or "").strip()[:500],
        "cases": _renumber([normalize_case(case) for case in (cases or []) if isinstance(case, dict)]),
        "created_at": now,
        "updated_at": now,
    }
    if _use_db():
        db.save_case_set(record)
        return record
    records = _read_file_store()
    records.append(record)
    _write_file_store(records)
    return record


def update_set(set_id: str, *, name: str | None = None, description: str | None = None) -> dict | None:
    record = get_set(set_id)
    if not record:
        return None
    if name is not None:
        cleaned = str(name).strip()
        if not cleaned:
            raise ValueError("用例集名称不能为空。")
        record["name"] = cleaned[:120]
    if description is not None:
        record["description"] = str(description).strip()[:500]
    record["updated_at"] = _now()
    _persist(record)
    return record


def delete_set(set_id: str) -> bool:
    if _use_db():
        return db.delete_case_set(set_id)
    records = _read_file_store()
    remaining = [record for record in records if record.get("id") != set_id]
    if len(remaining) == len(records):
        return False
    _write_file_store(remaining)
    return True


def add_cases(set_id: str, cases: list[dict], **provenance) -> dict | None:
    """把用例并入用例集，按「模块 + 用例名称」去重。返回新增/跳过统计。"""
    record = get_set(set_id)
    if not record:
        return None
    existing = record.get("cases") or []
    seen = {_dedupe_key(case) for case in existing}
    added = 0
    skipped = 0
    for case in cases:
        if not isinstance(case, dict):
            continue
        normalized = normalize_case(case, **provenance)
        key = _dedupe_key(normalized)
        if key in seen or not normalized["name"]:
            skipped += 1
            continue
        seen.add(key)
        existing.append(normalized)
        added += 1
    record["cases"] = _renumber(existing)
    record["updated_at"] = _now()
    _persist(record)
    return {"added": added, "skipped": skipped, "total": len(existing), "set": record}


def remove_cases(set_id: str, case_ids: list[str]) -> dict | None:
    record = get_set(set_id)
    if not record:
        return None
    targets = {str(case_id).strip() for case_id in case_ids if str(case_id).strip()}
    if not targets:
        return {"removed": 0, "total": len(record.get("cases") or []), "set": record}
    remaining = [case for case in (record.get("cases") or []) if case.get("id") not in targets]
    removed = len(record.get("cases") or []) - len(remaining)
    record["cases"] = _renumber(remaining)
    record["updated_at"] = _now()
    _persist(record)
    return {"removed": removed, "total": len(remaining), "set": record}


def _persist(record: dict) -> None:
    if _use_db():
        db.save_case_set(record)
        return
    records = _read_file_store()
    for index, item in enumerate(records):
        if item.get("id") == record.get("id"):
            records[index] = record
            break
    else:
        records.append(record)
    _write_file_store(records)
