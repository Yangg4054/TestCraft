"""TestCraft — AI-powered test case generator."""

import json
import logging
import os
import re
import tempfile
import time
import uuid
import asyncio
import threading
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from config import (
    load_config,
    save_config,
    env_locked_fields,
    mask_secret,
    uses_database,
    PROVIDER_DEFAULTS,
    SECRET_FIELDS,
)
from services.doc_parser import parse_document, is_feishu_url
from services.code_analyzer import analyze_code
from services.ai_generator import generate_test_cases
from services.ai_generator import split_requirement
from services.ai_generator import generate_case_points
from services.export import export_excel, export_markdown
from services.feishu_writer import create_test_case_doc
from services.test_script_gen import generate_test_scripts

import case_sets
import db

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "testcraft-dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("TESTCRAFT_UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
OUTPUT_DIR = os.environ.get("TESTCRAFT_OUTPUT_DIR", os.path.join(BASE_DIR, "outputs"))
REQUIREMENTS_FILE = os.environ.get(
    "TESTCRAFT_REQUIREMENTS_FILE", os.path.join(OUTPUT_DIR, "requirements.json")
)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Local-file fallback store, used only when no database is configured. Replaces the
# old hard-coded absolute OUTPUT_DIR so the image is portable.
FILE_STORE_DIR = os.environ.get(
    "TESTCRAFT_FILE_STORE", os.path.join(tempfile.gettempdir(), "testcraft_runs")
)
os.makedirs(FILE_STORE_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Persistence backend: PostgreSQL when a DB URL is set (requirement docs + generated
# test-case sets survive restarts and multiple replicas), else legacy local files.
DB_ENABLED = db.is_enabled()
if DB_ENABLED:
    db.init_db()
    logger.info("Persistence backend: PostgreSQL")
else:
    logger.warning(
        "Persistence backend: local files at %s — set TESTCRAFT_DATABASE_URL to use PostgreSQL",
        FILE_STORE_DIR,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED_DOC_EXTENSIONS = {".docx", ".pdf", ".md", ".markdown", ".txt"}
ALLOWED_CODE_EXTENSIONS = {".zip"}
REQUIREMENT_STATUSES = ("待分析", "分析中", "已拆分", "已完成")
PRIORITIES = ("P0", "P1", "P2", "P3")


def _allowed_doc(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_DOC_EXTENSIONS


def _allowed_code(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_CODE_EXTENSIONS


def _save_upload(file_storage, subdir: str = "") -> str:
    """Save an uploaded file and return its path."""
    safe_name = f"{uuid.uuid4().hex}_{file_storage.filename}"
    dest_dir = os.path.join(UPLOAD_DIR, subdir) if subdir else UPLOAD_DIR
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, safe_name)
    file_storage.save(dest)
    return dest


def _cleanup_old_files(directory: str, max_age_seconds: int = 3600) -> None:
    """Remove files older than max_age_seconds."""
    now = time.time()
    try:
        for entry in os.scandir(directory):
            if os.path.abspath(entry.path) == os.path.abspath(REQUIREMENTS_FILE):
                continue
            if entry.is_file() and (now - entry.stat().st_mtime) > max_age_seconds:
                os.unlink(entry.path)
                logger.debug("Cleaned up: %s", entry.path)
    except OSError:
        pass


def _load_requirements() -> list[dict]:
    """Load the requirement pool from its local JSON store."""
    if not os.path.exists(REQUIREMENTS_FILE):
        return []
    try:
        with open(REQUIREMENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        logger.exception("Failed to load requirements pool")
        return []


def _save_requirements(requirements: list[dict]) -> None:
    os.makedirs(os.path.dirname(REQUIREMENTS_FILE), exist_ok=True)
    temp_path = f"{REQUIREMENTS_FILE}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(requirements, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, REQUIREMENTS_FILE)


def _find_requirement(requirement_id: str) -> tuple[list[dict], dict | None]:
    requirements = _load_requirements()
    for requirement in requirements:
        if requirement.get("id") == requirement_id:
            return requirements, requirement
    return requirements, None


def _find_feature_point(requirement: dict, feature_id: str) -> dict | None:
    return next(
        (
            point for point in requirement.get("feature_points", []) or []
            if isinstance(point, dict) and point.get("id") == feature_id
        ),
        None,
    )


def _new_requirement(
    title: str,
    content: str,
    *,
    priority: str = "P1",
    source: str = "手动录入",
    source_url: str = "",
) -> dict:
    """Build a requirement-pool record. Single place that defines the shape."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "content": content,
        "priority": priority if priority in PRIORITIES else "P1",
        "status": "待分析",
        "source": source,
        "source_url": source_url,
        "feature_points": [],
        "created_at": now,
        "updated_at": now,
    }


def _title_from_document(text: str, fallback: str = "未命名需求") -> str:
    """Use the first Markdown heading or non-empty line as a document title."""
    for line in text.splitlines():
        value = re.sub(r"^#+\s*", "", line).strip()
        if value:
            return value[:120]
    return fallback


def _json_response(payload: dict | list, status: int = 200):
    return json.dumps(payload, ensure_ascii=False), status, {"Content-Type": "application/json"}


def _store_test_case_run(test_cases: list[dict], **context) -> dict:
    """Persist one generated suite and make it the session's active run."""
    if not isinstance(test_cases, list) or not test_cases:
        raise ValueError("未生成有效测试用例，请重试。")

    run_id = uuid.uuid4().hex[:12]
    _store_run(run_id, test_cases, feishu_doc_url="", **context)
    session["run_id"] = run_id
    session["feishu_doc_url"] = ""
    return {"run_id": run_id}


def _remember_run_origin(
    *,
    requirement_id: str = "",
    requirement_title: str = "",
    feature_id: str = "",
) -> None:
    """Track where the active run came from, so saved cases stay traceable."""
    session["run_origin"] = {
        "requirement_id": requirement_id,
        "requirement_title": requirement_title,
        "feature_id": feature_id,
    }


def _feature_point_requirement_text(
    requirement: dict,
    feature_point: dict,
    all_requirements: list[dict],
) -> str:
    """Build a focused, structured source document for one feature point."""
    related = []
    dependency_ids = {
        str(item.get("requirement_id", "")).strip()
        for item in feature_point.get("requirement_dependencies", [])
        if isinstance(item, dict)
    }
    dependency_titles = {
        str(item.get("requirement_title", "")).strip()
        for item in feature_point.get("requirement_dependencies", [])
        if isinstance(item, dict)
    }
    for item in all_requirements:
        if item.get("id") in dependency_ids or item.get("title") in dependency_titles:
            related.append({
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "content": str(item.get("content", ""))[:4000],
            })

    case_points = [
        point for point in (feature_point.get("case_points") or []) if isinstance(point, dict)
    ]
    source = {
        "current_requirement": {
            "id": requirement.get("id", ""),
            "title": requirement.get("title", ""),
            "content": requirement.get("content", ""),
        },
        "selected_feature_point": feature_point,
        "dependency_requirements": related,
        "test_scope": (
            "只针对 selected_feature_point 生成用例，覆盖主流程、异常流程、业务规则、"
            "状态与数据变化、边界、权限以及已识别的跨需求依赖。"
        ),
    }
    if case_points:
        # 已完成第二阶段（生成 Case 点）时，用例必须逐条落到测试点上，保证可追溯。
        source["case_points"] = case_points
        source["test_scope"] = (
            "case_points 是本功能点已评审的测试点清单。请为每个测试点生成 1~3 条可执行用例，"
            "覆盖其 conditions 与 verify_points，并在用例的 module 字段保留功能点名称。"
            "不得遗漏任何测试点，也不要生成与测试点无关的用例。"
        )
    return json.dumps(source, ensure_ascii=False, indent=2)[:30000]


# --- Persistence facade: PostgreSQL when configured, else local JSON files -----

def _store_run(
    run_id: str,
    test_cases: list,
    *,
    requirement_text: str | None = None,
    requirement_source: str | None = None,
    code_structure_text: str | None = None,
    feishu_doc_url: str | None = None,
) -> None:
    if DB_ENABLED:
        db.save_run(
            run_id,
            test_cases,
            requirement_text=requirement_text,
            requirement_source=requirement_source,
            code_structure_text=code_structure_text,
            feishu_doc_url=feishu_doc_url,
        )
    else:
        path = os.path.join(FILE_STORE_DIR, f"testcases_{run_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(test_cases, f, ensure_ascii=False)


def _load_run(run_id: str | None) -> dict | None:
    """Return a run dict (with at least test_cases + feishu_doc_url), or None."""
    if not run_id:
        return None
    if DB_ENABLED:
        return db.get_run(run_id)
    path = os.path.join(FILE_STORE_DIR, f"testcases_{run_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return {"run_id": run_id, "test_cases": json.load(f), "feishu_doc_url": ""}


def _list_runs() -> list:
    if DB_ENABLED:
        return db.list_runs()
    records = []
    for fn in sorted(os.listdir(FILE_STORE_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        run_id = fn[len("testcases_"):-len(".json")]
        fpath = os.path.join(FILE_STORE_DIR, fn)
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                count = len(json.load(fh))
        except Exception:
            count = 0
        records.append({
            "run_id": run_id,
            "count": count,
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.stat(fpath).st_mtime)),
        })
    return records


def _update_feishu(run_id: str | None, url: str) -> None:
    if DB_ENABLED and run_id:
        db.update_feishu_url(run_id, url)


def _make_export(test_cases: list, fmt: str) -> str:
    """Regenerate an export artifact from stored test cases into a temp file."""
    suffix = ".xlsx" if fmt == "excel" else ".md"
    fd, path = tempfile.mkstemp(prefix="tc_export_", suffix=suffix)
    os.close(fd)
    if fmt == "excel":
        export_excel(test_cases, path)
    else:
        export_markdown(test_cases, path)
    return path


@app.context_processor
def inject_nav_state():
    """Counters shown next to the three workflow stages in the sidebar."""
    try:
        requirements = _load_requirements()
    except Exception:
        logger.exception("Failed to load requirements for navigation")
        requirements = []
    features = [
        point
        for item in requirements
        for point in (item.get("feature_points") or [])
        if isinstance(point, dict)
    ]
    try:
        set_count = len(case_sets.list_sets())
    except Exception:
        logger.exception("Failed to load case sets for navigation")
        set_count = 0
    return {
        "nav_counts": {
            "requirements": len(requirements),
            "features": len(features),
            "case_points": sum(len(point.get("case_points") or []) for point in features),
            "case_sets": set_count,
        }
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    _cleanup_old_files(UPLOAD_DIR)
    return render_template("index.html")


@app.route("/requirements")
def requirements_pool():
    """List requirements in the local requirement pool."""
    requirements = sorted(
        _load_requirements(), key=lambda item: item.get("updated_at", ""), reverse=True
    )
    status = request.args.get("status", "").strip()
    query = request.args.get("q", "").strip()
    if status:
        requirements = [item for item in requirements if item.get("status") == status]
    if query:
        needle = query.casefold()
        requirements = [
            item for item in requirements
            if needle in f"{item.get('title', '')} {item.get('content', '')}".casefold()
        ]
    return render_template(
        "requirements.html", requirements=requirements, status=status, query=query
    )


@app.route("/requirements", methods=["POST"])
def create_requirement():
    source_type = request.form.get("source_type", "manual").strip().lower()
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    feishu_url = request.form.get("feishu_url", "").strip()
    priority = request.form.get("priority", "P1").strip().upper()
    source = request.form.get("source", "手动录入").strip() or "手动录入"

    if source_type == "feishu":
        if not feishu_url or not is_feishu_url(feishu_url):
            flash("请输入有效的飞书文档或 Wiki 链接。", "danger")
            return redirect(url_for("requirements_pool"))
        try:
            content = parse_document(feishu_url).strip()
        except ValueError as e:
            flash(f"读取飞书文档失败：{e}", "danger")
            return redirect(url_for("requirements_pool"))
        if not content:
            flash("飞书文档内容为空，无法创建需求。", "danger")
            return redirect(url_for("requirements_pool"))
        title = title or _title_from_document(content)
        source = "飞书文档"
    elif not title or not content:
        flash("需求标题和需求内容不能为空。", "danger")
        return redirect(url_for("requirements_pool"))
    if priority not in {"P0", "P1", "P2", "P3"}:
        priority = "P1"

    requirement = _new_requirement(
        title,
        content,
        priority=priority,
        source=source,
        source_url=feishu_url if source_type == "feishu" else "",
    )
    requirements = _load_requirements()
    requirements.append(requirement)
    _save_requirements(requirements)
    flash("需求已加入需求池。", "success")
    return redirect(url_for("requirement_detail", requirement_id=requirement["id"]))


@app.route("/requirements/<requirement_id>")
def requirement_detail(requirement_id: str):
    _, requirement = _find_requirement(requirement_id)
    if not requirement:
        flash("需求不存在。", "warning")
        return redirect(url_for("requirements_pool"))
    return render_template("requirement_detail.html", requirement=requirement)


@app.route("/requirements/<requirement_id>/update", methods=["POST"])
def update_requirement(requirement_id: str):
    """Edit a requirement in place. Feature points and case points are kept."""
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        flash("需求不存在。", "warning")
        return redirect(url_for("requirements_pool"))

    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    if not title or not content:
        flash("需求标题和需求内容不能为空。", "danger")
        return redirect(url_for("requirement_detail", requirement_id=requirement_id))

    priority = request.form.get("priority", requirement.get("priority", "P1")).strip().upper()
    status = request.form.get("status", requirement.get("status", "待分析")).strip()
    requirement["title"] = title[:200]
    requirement["content"] = content
    requirement["priority"] = priority if priority in PRIORITIES else requirement.get("priority", "P1")
    requirement["status"] = status if status in REQUIREMENT_STATUSES else requirement.get("status", "待分析")
    requirement["source"] = request.form.get("source", requirement.get("source", "")).strip()
    requirement["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_requirements(requirements)
    flash("需求已更新。", "success")
    return redirect(url_for("requirement_detail", requirement_id=requirement_id))


@app.route("/requirements/<requirement_id>/delete", methods=["POST"])
def delete_requirement(requirement_id: str):
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        flash("需求不存在。", "warning")
        return redirect(url_for("requirements_pool"))
    remaining = [item for item in requirements if item.get("id") != requirement_id]
    _save_requirements(remaining)
    flash(f"已删除需求「{requirement.get('title', '')}」。", "success")
    return redirect(url_for("requirements_pool"))


@app.route("/api/requirements/<requirement_id>/split", methods=["POST"])
def api_split_requirement(requirement_id: str):
    """Split one requirement into structured feature points with AI."""
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        return _json_response({"error": "需求不存在"}, 404)

    try:
        feature_points = split_requirement(
            requirement.get("title", ""),
            requirement.get("content", ""),
            [item for item in requirements if item.get("id") != requirement_id],
        )
        requirement["feature_points"] = feature_points
        requirement["status"] = "已拆分"
        requirement["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_requirements(requirements)
        return _json_response({"feature_points": feature_points, "count": len(feature_points)})
    except ValueError as e:
        logger.warning("Requirement split failed: %s", e)
        return _json_response({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("Requirement split failed")
        return _json_response({"error": f"拆分失败：{e}"}, 500)


@app.route(
    "/api/requirements/<requirement_id>/features/<feature_id>/case-points",
    methods=["POST"],
)
def api_generate_case_points(requirement_id: str, feature_id: str):
    """Stage 2: expand one feature point into reviewable test points (Case 点)."""
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        return _json_response({"error": "需求不存在"}, 404)
    feature_point = _find_feature_point(requirement, feature_id)
    if not feature_point:
        return _json_response({"error": "功能点不存在"}, 404)

    dependency_ids = {
        str(item.get("requirement_id", "")).strip()
        for item in feature_point.get("requirement_dependencies", []) or []
        if isinstance(item, dict)
    }
    related = [item for item in requirements if item.get("id") in dependency_ids]

    try:
        points = generate_case_points(requirement, feature_point, related)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        feature_point["case_points"] = points
        feature_point["case_points_updated_at"] = now
        requirement["updated_at"] = now
        _save_requirements(requirements)
        return _json_response({"case_points": points, "count": len(points)})
    except ValueError as e:
        logger.warning("Case point generation failed: %s", e)
        return _json_response({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("Case point generation failed")
        return _json_response({"error": f"生成 Case 点失败：{e}"}, 500)


@app.route(
    "/api/requirements/<requirement_id>/features/<feature_id>/case-points/<case_point_id>",
    methods=["DELETE"],
)
def api_delete_case_point(requirement_id: str, feature_id: str, case_point_id: str):
    """Drop one reviewed-out test point before generating cases."""
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        return _json_response({"error": "需求不存在"}, 404)
    feature_point = _find_feature_point(requirement, feature_id)
    if not feature_point:
        return _json_response({"error": "功能点不存在"}, 404)

    points = feature_point.get("case_points") or []
    remaining = [point for point in points if point.get("id") != case_point_id]
    if len(remaining) == len(points):
        return _json_response({"error": "Case 点不存在"}, 404)
    feature_point["case_points"] = remaining
    requirement["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_requirements(requirements)
    return _json_response({"count": len(remaining)})


@app.route(
    "/api/requirements/<requirement_id>/features/<feature_id>/generate-cases",
    methods=["POST"],
)
def api_generate_feature_cases(requirement_id: str, feature_id: str):
    """Stage 3: generate and persist a focused test suite for one feature point."""
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        return _json_response({"error": "需求不存在"}, 404)

    feature_point = _find_feature_point(requirement, feature_id)
    if not feature_point:
        return _json_response({"error": "功能点不存在"}, 404)

    case_point_count = len([
        point for point in (feature_point.get("case_points") or []) if isinstance(point, dict)
    ])
    # 已评审过 Case 点时，用例规模按测试点数量放大（每个测试点 1~2 条）。
    target_count = max(12, min(case_point_count * 2, 60)) if case_point_count else 12
    min_count = max(6, case_point_count) if case_point_count else 6

    try:
        source_text = _feature_point_requirement_text(
            requirement, feature_point, requirements
        )
        test_cases = generate_test_cases(
            source_text, target_count=target_count, min_count=min_count
        )
        run = _store_test_case_run(
            test_cases,
            requirement_text=source_text,
            requirement_source=f"{requirement.get('title', '')} / {feature_point.get('name', '')}",
        )
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        feature_point["test_case_run_id"] = run["run_id"]
        feature_point["test_case_count"] = len(test_cases)
        feature_point["test_cases_updated_at"] = now
        requirement["updated_at"] = now
        _save_requirements(requirements)
        _remember_run_origin(
            requirement_id=requirement_id,
            requirement_title=requirement.get("title", ""),
            feature_id=feature_id,
        )
        return _json_response({
            "run_id": run["run_id"],
            "count": len(test_cases),
            "case_point_count": case_point_count,
            "redirect": url_for("results"),
        })
    except ValueError as e:
        logger.warning("Feature test case generation failed: %s", e)
        return _json_response({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("Feature test case generation failed")
        return _json_response({"error": f"生成用例失败：{e}"}, 500)


@app.route("/api/requirements/<requirement_id>/status", methods=["POST"])
def api_update_requirement_status(requirement_id: str):
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        return _json_response({"error": "需求不存在"}, 404)
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip()
    if status not in {"待分析", "分析中", "已拆分", "已完成"}:
        return _json_response({"error": "无效的需求状态"}, 400)
    requirement["status"] = status
    requirement["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_requirements(requirements)
    return _json_response({"status": status})


@app.route("/case-points")
def case_points_overview():
    """Stage 2 workspace: every split feature point and its reviewed test points."""
    requirements = sorted(
        _load_requirements(), key=lambda item: item.get("updated_at", ""), reverse=True
    )
    split_requirements = [item for item in requirements if item.get("feature_points")]
    selected_id = request.args.get("requirement", "").strip()
    selected = next(
        (item for item in split_requirements if item.get("id") == selected_id),
        split_requirements[0] if split_requirements else None,
    )
    totals = {
        "requirements": len(split_requirements),
        "features": sum(len(item.get("feature_points") or []) for item in split_requirements),
        "case_points": sum(
            len(point.get("case_points") or [])
            for item in split_requirements
            for point in (item.get("feature_points") or [])
        ),
        "pending": sum(
            1
            for item in split_requirements
            for point in (item.get("feature_points") or [])
            if not (point.get("case_points") or [])
        ),
    }
    return render_template(
        "case_points.html",
        requirements=split_requirements,
        selected=selected,
        totals=totals,
        unsplit_count=len(requirements) - len(split_requirements),
    )


# ---------------------------------------------------------------------------
# 用例集（测试资产）
# ---------------------------------------------------------------------------

@app.route("/case-sets")
def case_set_list():
    return render_template("case_sets.html", case_set_list=case_sets.list_sets())


@app.route("/case-sets", methods=["POST"])
def create_case_set():
    try:
        record = case_sets.create_set(
            request.form.get("name", ""),
            request.form.get("description", ""),
        )
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("case_set_list"))
    flash(f"用例集「{record['name']}」已创建。", "success")
    return redirect(url_for("case_set_detail", set_id=record["id"]))


@app.route("/case-sets/<set_id>")
def case_set_detail(set_id: str):
    record = case_sets.get_set(set_id)
    if not record:
        flash("用例集不存在。", "warning")
        return redirect(url_for("case_set_list"))
    return render_template("case_set_detail.html", case_set=record)


@app.route("/case-sets/<set_id>/update", methods=["POST"])
def update_case_set(set_id: str):
    try:
        record = case_sets.update_set(
            set_id,
            name=request.form.get("name", ""),
            description=request.form.get("description", ""),
        )
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("case_set_detail", set_id=set_id))
    if not record:
        flash("用例集不存在。", "warning")
        return redirect(url_for("case_set_list"))
    flash("用例集已更新。", "success")
    return redirect(url_for("case_set_detail", set_id=set_id))


@app.route("/case-sets/<set_id>/delete", methods=["POST"])
def delete_case_set(set_id: str):
    if not case_sets.delete_set(set_id):
        flash("用例集不存在。", "warning")
    else:
        flash("用例集已删除。", "success")
    return redirect(url_for("case_set_list"))


@app.route("/api/case-sets", methods=["GET"])
def api_list_case_sets():
    return _json_response({"case_sets": case_sets.list_sets()})


@app.route("/api/case-sets/<set_id>/cases", methods=["POST"])
def api_add_cases_to_set(set_id: str):
    """Append selected cases from the active run into a case set."""
    data = request.get_json(silent=True) or {}
    case_ids = {str(value).strip() for value in data.get("case_ids", []) if str(value).strip()}
    run = _load_run(session.get("run_id"))
    if not run or not run.get("test_cases"):
        return _json_response({"error": "没有可保存的测试用例，请先生成。"}, 400)

    selected = [
        case for case in run["test_cases"]
        if not case_ids or str(case.get("id", "")) in case_ids
    ]
    if not selected:
        return _json_response({"error": "请至少选择一条测试用例。"}, 400)

    origin = session.get("run_origin") or {}
    result = case_sets.add_cases(
        set_id,
        selected,
        source_run_id=session.get("run_id", ""),
        source_requirement_id=origin.get("requirement_id", ""),
        source_requirement_title=origin.get("requirement_title", ""),
        source_feature_id=origin.get("feature_id", ""),
    )
    if result is None:
        return _json_response({"error": "用例集不存在"}, 404)
    return _json_response({
        "added": result["added"],
        "skipped": result["skipped"],
        "total": result["total"],
        "set_id": set_id,
        "set_name": result["set"].get("name", ""),
        "redirect": url_for("case_set_detail", set_id=set_id),
    })


@app.route("/api/case-sets", methods=["POST"])
def api_create_case_set():
    """Create a case set inline (used by the '保存到用例集' dialog)."""
    data = request.get_json(silent=True) or {}
    try:
        record = case_sets.create_set(data.get("name", ""), data.get("description", ""))
    except ValueError as e:
        return _json_response({"error": str(e)}, 400)
    return _json_response({"id": record["id"], "name": record["name"]})


@app.route("/api/case-sets/<set_id>/cases/remove", methods=["POST"])
def api_remove_cases_from_set(set_id: str):
    data = request.get_json(silent=True) or {}
    result = case_sets.remove_cases(set_id, data.get("case_ids", []))
    if result is None:
        return _json_response({"error": "用例集不存在"}, 404)
    return _json_response({"removed": result["removed"], "total": result["total"]})


@app.route("/case-sets/<set_id>/download/<fmt>")
def download_case_set(set_id: str, fmt: str):
    record = case_sets.get_set(set_id)
    if not record or not record.get("cases"):
        flash("用例集为空，无法导出。", "warning")
        return redirect(url_for("case_set_detail", set_id=set_id))
    if fmt not in {"excel", "markdown"}:
        flash("不支持的导出格式。", "warning")
        return redirect(url_for("case_set_detail", set_id=set_id))
    path = _make_export(record["cases"], fmt)
    extension = "xlsx" if fmt == "excel" else "md"
    return send_file(
        path, as_attachment=True, download_name=f"{record['name']}.{extension}"
    )


@app.route("/configure", methods=["GET", "POST"])
def configure():
    if request.method == "POST":
        config = {
            "provider": request.form.get("provider", "openai"),
            "base_url": request.form.get("base_url", "").strip(),
            "model": request.form.get("model", "").strip(),
            "feishu_app_id": request.form.get("feishu_app_id", "").strip(),
            "feishu_domain": request.form.get("feishu_domain", "https://open.feishu.cn").strip(),
        }
        # 密钥字段留空表示"保持不变"：页面不回显密钥，避免旧值被空值覆盖。
        for field in SECRET_FIELDS:
            submitted = request.form.get(field, "").strip()
            config[field] = submitted if submitted else None

        try:
            save_config(config)
        except Exception as e:
            logger.exception("Failed to save configuration")
            flash(f"配置保存失败：{e}", "danger")
            return redirect(url_for("configure"))

        flash(
            "配置已保存到数据库，重启和重新部署都不会丢失。" if uses_database()
            else "配置已保存到本地 config.json；未配置数据库时容器重启会丢失。",
            "success" if uses_database() else "warning",
        )
        return redirect(url_for("configure"))

    config = load_config(use_cache=False)
    return render_template(
        "configure.html",
        config=config,
        provider_defaults=PROVIDER_DEFAULTS,
        secret_hints={field: mask_secret(config.get(field, "")) for field in SECRET_FIELDS},
        uses_database=uses_database(),
        env_locked=env_locked_fields(),
    )


def _archive_uploaded_requirement(text: str, source: str | None) -> dict | None:
    """Save an uploaded/imported requirement document into the requirement pool.

    上传的需求文档以前只作为一次性输入，生成完就丢失；现在统一入池，
    可以继续走「需求拆分 → Case 点 → 用例」的流程。同一份文档重复上传时复用已有记录。
    """
    text = (text or "").strip()
    if not text:
        return None
    source = (source or "").strip()
    title = _title_from_document(text)
    try:
        requirements = _load_requirements()
        for item in requirements:
            if item.get("content", "").strip() == text:
                item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_requirements(requirements)
                return item
        is_url = source.startswith("http://") or source.startswith("https://")
        requirement = _new_requirement(
            title,
            text,
            source=("飞书文档" if is_url else (source or "文档上传")),
            source_url=source if is_url else "",
        )
        requirements.append(requirement)
        _save_requirements(requirements)
        return requirement
    except OSError:
        # 需求池落盘失败不应该阻断用例生成主流程。
        logger.exception("Failed to archive uploaded requirement")
        return None


@app.route("/generate", methods=["POST"])
def generate():
    try:
        # --- Parse requirements document ---
        requirements_text = None
        requirement_source = None

        # Option 1: Feishu document URL
        feishu_url = request.form.get("feishu_url", "").strip()
        if feishu_url:
            if is_feishu_url(feishu_url):
                try:
                    requirements_text = parse_document(feishu_url)
                    requirement_source = feishu_url
                except ValueError as e:
                    flash(f"Failed to parse Feishu document: {e}", "danger")
                    return redirect(url_for("index"))
            else:
                flash("Invalid Feishu document URL. Expected format: https://xxx.feishu.cn/docx/TOKEN or /wiki/TOKEN", "danger")
                return redirect(url_for("index"))

        # Option 2: Uploaded file
        if not requirements_text:
            doc_file = request.files.get("doc_file")
            if doc_file and doc_file.filename:
                if not _allowed_doc(doc_file.filename):
                    flash("Unsupported document format. Use .docx, .pdf, .md, or .txt.", "danger")
                    return redirect(url_for("index"))
                doc_path = _save_upload(doc_file, "docs")
                requirements_text = parse_document(doc_path)
                requirement_source = doc_file.filename

        if not requirements_text:
            flash("Please upload a requirements document.", "danger")
            return redirect(url_for("index"))

        # --- Analyze code (optional) ---
        code_structure_text = None
        code_file = request.files.get("code_file")
        code_path_input = request.form.get("code_path", "").strip()

        if code_file and code_file.filename:
            if not _allowed_code(code_file.filename):
                flash("Code upload must be a .zip file.", "danger")
                return redirect(url_for("index"))
            zip_path = _save_upload(code_file, "code")
            structure = analyze_code(zip_path)
            code_structure_text = structure.to_text()
        elif code_path_input:
            if not os.path.isdir(code_path_input):
                flash(f"Directory not found: {code_path_input}", "danger")
                return redirect(url_for("index"))
            structure = analyze_code(code_path_input)
            code_structure_text = structure.to_text()

        # --- 上传的需求一律沉淀进需求池，避免"生成完就丢" ---
        saved_requirement = _archive_uploaded_requirement(
            requirements_text, requirement_source
        )

        # --- Generate test cases ---
        test_cases = generate_test_cases(requirements_text, code_structure_text)

        # --- Create Feishu document (best effort) ---
        run_id = uuid.uuid4().hex[:12]
        try:
            feishu_doc_url = create_test_case_doc(test_cases, f"测试用例 - {run_id}")
        except Exception as e:
            logger.warning("Failed to create Feishu document: %s", e)
            feishu_doc_url = ""

        # --- Persist the run (requirement doc + generated test-case set) ---
        _store_run(
            run_id,
            test_cases,
            requirement_text=requirements_text,
            requirement_source=requirement_source,
            code_structure_text=code_structure_text,
            feishu_doc_url=feishu_doc_url,
        )
        session["run_id"] = run_id
        session["feishu_doc_url"] = feishu_doc_url
        if saved_requirement:
            saved_requirement_list, stored = _find_requirement(saved_requirement["id"])
            if stored:
                stored["test_case_run_id"] = run_id
                stored["test_case_count"] = len(test_cases)
                stored["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_requirements(saved_requirement_list)
            _remember_run_origin(
                requirement_id=saved_requirement["id"],
                requirement_title=saved_requirement.get("title", ""),
            )
        else:
            _remember_run_origin()

        return redirect(url_for("results"))

    except ValueError as e:
        logger.warning("Validation error: %s", e)
        flash(str(e), "danger")
        return redirect(url_for("index"))
    except Exception as e:
        logger.exception("Generation failed")
        flash(f"An error occurred: {e}", "danger")
        return redirect(url_for("index"))


@app.route("/results")
def results():
    run = _load_run(session.get("run_id"))
    test_cases = run["test_cases"] if run else None
    if not test_cases:
        flash("No test cases to display. Please generate first.", "warning")
        return redirect(url_for("index"))
    feishu_doc_url = (run.get("feishu_doc_url") or session.get("feishu_doc_url", "")) if run else ""
    return render_template(
        "results.html",
        test_cases=test_cases,
        feishu_doc_url=feishu_doc_url,
        excel_path="",
        case_set_list=case_sets.list_sets(),
        run_origin=session.get("run_origin") or {},
    )


@app.route("/run-test")
def run_test():
    return render_template("run_test.html", excel_path="")


@app.route("/download/<fmt>")
def download(fmt: str):
    run = _load_run(session.get("run_id"))
    if run and run.get("test_cases"):
        if fmt == "excel":
            path = _make_export(run["test_cases"], "excel")
            return send_file(path, as_attachment=True, download_name="test_cases.xlsx")
        elif fmt == "markdown":
            path = _make_export(run["test_cases"], "markdown")
            return send_file(path, as_attachment=True, download_name="test_cases.md")

    flash("File not found. Please regenerate test cases.", "warning")
    return redirect(url_for("results"))


@app.route("/api/create-feishu-doc", methods=["POST"])
def api_create_feishu_doc():
    """Create a Feishu document from the current session's test cases."""
    run_id = session.get("run_id")
    run = _load_run(run_id)
    if not run or not run.get("test_cases"):
        return json.dumps({"error": "没有测试用例数据"}), 400, {"Content-Type": "application/json"}
    try:
        feishu_url = create_test_case_doc(run["test_cases"], f"测试用例 - {run_id}")
        _update_feishu(run_id, feishu_url)
        session["feishu_doc_url"] = feishu_url
        return json.dumps({"url": feishu_url}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning("Failed to create Feishu doc: %s", e)
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/generate-scripts", methods=["POST"])
def api_generate_scripts():
    """Generate executable test scripts from test cases + code paths."""
    run = _load_run(session.get("run_id"))
    if not run or not run.get("test_cases"):
        return json.dumps({"error": "没有测试用例数据，请先生成测试用例"}), 400, {"Content-Type": "application/json"}

    data = request.get_json()
    code_paths = data.get("code_paths", [])
    if not code_paths:
        return json.dumps({"error": "请提供至少一个代码路径"}), 400, {"Content-Type": "application/json"}

    try:
        result = generate_test_scripts(run["test_cases"], code_paths)
        # result has: architecture, scripts, project_dir
        session["test_project_dir"] = result["project_dir"]
        return json.dumps({
            "architecture": result["architecture"],
            "scripts": result["scripts"],
            "project_dir": result["project_dir"],
        }, ensure_ascii=False), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.exception("Script generation failed")
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/regenerate", methods=["POST"])
def api_regenerate():
    """Regenerate test cases with user feedback."""
    from services.ai_generator import (
        COVERAGE_SYSTEM_PROMPT,
        MIN_CASE_COUNT,
        SYSTEM_PROMPT,
        _merge_test_cases,
        _parse_response,
        call_llm,
    )

    old_run = _load_run(session.get("run_id"))
    if not old_run or not old_run.get("test_cases"):
        return json.dumps({"error": "没有测试用例数据"}), 400, {"Content-Type": "application/json"}

    data = request.get_json()
    feedback = data.get("feedback", "").strip()
    if not feedback:
        return json.dumps({"error": "请输入修改描述"}), 400, {"Content-Type": "application/json"}

    old_cases = old_run["test_cases"]
    old_cases_text = json.dumps(old_cases[:30], ensure_ascii=False, indent=2)
    user_content = f"""## 已有测试用例
{old_cases_text}

## 用户反馈
{feedback}

请根据用户反馈修改和完善上述测试用例。保留好的用例，修改有问题的，补充缺失的。
Return ONLY a JSON array of test case objects."""

    try:
        content = call_llm(SYSTEM_PROMPT, user_content)
        test_cases = _parse_response(content)
        if len(test_cases) < MIN_CASE_COUNT:
            coverage_content = f"""## Existing Test Cases
{json.dumps(test_cases, ensure_ascii=False, indent=2)}

## User Feedback
{feedback}

Generate additional cases for every missing requirement and scenario. Return only the JSON object wrapper."""
            extra_cases = _parse_response(
                call_llm(COVERAGE_SYSTEM_PROMPT, coverage_content)
            )
            test_cases = _merge_test_cases(test_cases, extra_cases)

        # Save as a new run, carrying the original requirement context forward.
        run = _store_test_case_run(
            test_cases,
            requirement_text=old_run.get("requirement_text"),
            requirement_source=old_run.get("requirement_source"),
            code_structure_text=old_run.get("code_structure_text"),
        )
        run_id = run["run_id"]

        return json.dumps({"count": len(test_cases), "run_id": run_id}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.exception("Regeneration failed")
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/provider-defaults/<provider>")
def api_provider_defaults(provider: str):
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    return json.dumps(defaults), 200, {"Content-Type": "application/json"}


@app.route("/history")
def history():
    """List previously generated test case runs."""
    records = _list_runs()
    return render_template("history.html", records=records)


@app.route("/history/<run_id>")
def history_detail(run_id: str):
    """View a specific historical test case run."""
    if not re.fullmatch(r"[0-9a-fA-F]{12}", run_id):
        flash("记录编号无效。", "warning")
        return redirect(url_for("history"))

    run = _load_run(run_id)
    if not run or not run.get("test_cases"):
        flash("记录不存在。", "warning")
        return redirect(url_for("history"))
    # Make this run the active one so download / feishu / regenerate act on it.
    session["run_id"] = run_id
    session["feishu_doc_url"] = run.get("feishu_doc_url", "")
    session.pop("run_origin", None)
    return render_template(
        "results.html",
        test_cases=run["test_cases"],
        feishu_doc_url=run.get("feishu_doc_url", ""),
        excel_path="",
        case_set_list=case_sets.list_sets(),
        run_origin={},
    )


@app.route("/autotest")
def autotest():
    return render_template("autotest.html")


@app.route("/api/detect-project", methods=["POST"])
def api_detect_project():
    """Auto-detect frontend/backend code paths in a project directory."""
    data = request.get_json()
    root = data.get("root", "").strip()
    if not root or not os.path.isdir(root):
        return json.dumps({"error": f"目录不存在: {root}"}), 400, {"Content-Type": "application/json"}

    results = []
    frontend_markers = {
        "package.json": ["react", "vue", "angular", "next", "nuxt", "svelte"],
        "tsconfig.json": None,
    }
    backend_markers = {
        "requirements.txt": None, "setup.py": None, "pyproject.toml": None,
        "go.mod": None, "Cargo.toml": None, "pom.xml": None, "build.gradle": None,
        "main.py": None, "app.py": None, "server.py": None, "main.go": None,
    }

    for entry in os.scandir(root):
        if entry.is_dir() and not entry.name.startswith("."):
            subdir = entry.path
            label = _detect_dir_type(subdir, frontend_markers, backend_markers)
            if label:
                results.append({"path": subdir, "label": label, "name": entry.name})

    # Also check root itself
    root_label = _detect_dir_type(root, frontend_markers, backend_markers)
    if root_label and not results:
        results.append({"path": root, "label": root_label, "name": os.path.basename(root)})

    if not results:
        # Fallback: list subdirs with code files
        for entry in os.scandir(root):
            if entry.is_dir() and not entry.name.startswith(".") and entry.name not in ("node_modules", ".git", "__pycache__", ".venv", "venv"):
                results.append({"path": entry.path, "label": "未知", "name": entry.name})

    return json.dumps(results, ensure_ascii=False), 200, {"Content-Type": "application/json"}


def _detect_dir_type(dirpath, frontend_markers, backend_markers):
    try:
        files = set(os.listdir(dirpath))
    except OSError:
        return None

    # Check frontend
    for marker, keywords in frontend_markers.items():
        if marker in files:
            if keywords and marker == "package.json":
                try:
                    with open(os.path.join(dirpath, marker), "r") as f:
                        pkg = json.load(f)
                    deps = str(pkg.get("dependencies", {})) + str(pkg.get("devDependencies", {}))
                    if any(k in deps for k in keywords):
                        return "前端"
                except Exception:
                    pass
            elif not keywords:
                return "前端"

    # Check backend
    for marker in backend_markers:
        if marker in files:
            return "后端"

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 调试器默认关闭：debug=True 会暴露 Werkzeug 交互式控制台，
    # 监听 0.0.0.0 时等于对同网段开放远程代码执行。本地排查用 FLASK_DEBUG=1 显式打开。
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host="0.0.0.0", port=8899)
