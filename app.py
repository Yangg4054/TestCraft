"""TestCraft — AI-powered test case generator."""

import json
import logging
import os
import re
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

from config import load_config, save_config, PROVIDER_DEFAULTS
from services.doc_parser import parse_document, is_feishu_url
from services.code_analyzer import analyze_code
from services.ai_generator import generate_test_cases
from services.ai_generator import split_requirement
from services.export import export_excel, export_markdown
from services.feishu_writer import create_test_case_doc
from services.test_script_gen import generate_test_scripts

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
os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED_DOC_EXTENSIONS = {".docx", ".pdf", ".md", ".markdown", ".txt"}
ALLOWED_CODE_EXTENSIONS = {".zip"}


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


def _title_from_document(text: str, fallback: str = "未命名需求") -> str:
    """Use the first Markdown heading or non-empty line as a document title."""
    for line in text.splitlines():
        value = re.sub(r"^#+\s*", "", line).strip()
        if value:
            return value[:120]
    return fallback


def _json_response(payload: dict | list, status: int = 200):
    return json.dumps(payload, ensure_ascii=False), status, {"Content-Type": "application/json"}


def _store_test_case_run(test_cases: list[dict]) -> dict:
    """Persist one generated suite and expose it to result/download routes."""
    if not isinstance(test_cases, list) or not test_cases:
        raise ValueError("未生成有效测试用例，请重试。")

    run_id = uuid.uuid4().hex[:12]
    tc_json_path = os.path.join(OUTPUT_DIR, f"testcases_{run_id}.json")
    excel_path = os.path.join(OUTPUT_DIR, f"testcases_{run_id}.xlsx")
    md_path = os.path.join(OUTPUT_DIR, f"testcases_{run_id}.md")
    with open(tc_json_path, "w", encoding="utf-8") as f:
        json.dump(test_cases, f, ensure_ascii=False)
    export_excel(test_cases, excel_path)
    export_markdown(test_cases, md_path)

    session["run_id"] = run_id
    session["tc_json_path"] = tc_json_path
    session["excel_path"] = excel_path
    session["md_path"] = md_path
    session["feishu_doc_url"] = ""
    return {
        "run_id": run_id,
        "tc_json_path": tc_json_path,
        "excel_path": excel_path,
        "md_path": md_path,
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
    return json.dumps(source, ensure_ascii=False, indent=2)[:30000]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    _cleanup_old_files(UPLOAD_DIR)
    _cleanup_old_files(OUTPUT_DIR)
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

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    requirement = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "content": content,
        "priority": priority,
        "status": "待分析",
        "source": source,
        "source_url": feishu_url if source_type == "feishu" else "",
        "feature_points": [],
        "created_at": now,
        "updated_at": now,
    }
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
    "/api/requirements/<requirement_id>/features/<feature_id>/generate-cases",
    methods=["POST"],
)
def api_generate_feature_cases(requirement_id: str, feature_id: str):
    """Generate and persist a focused test suite for one feature point."""
    requirements, requirement = _find_requirement(requirement_id)
    if not requirement:
        return _json_response({"error": "需求不存在"}, 404)

    feature_point = next(
        (
            point for point in requirement.get("feature_points", [])
            if isinstance(point, dict) and point.get("id") == feature_id
        ),
        None,
    )
    if not feature_point:
        return _json_response({"error": "功能点不存在"}, 404)

    try:
        source_text = _feature_point_requirement_text(
            requirement, feature_point, requirements
        )
        test_cases = generate_test_cases(
            source_text, target_count=12, min_count=6
        )
        run = _store_test_case_run(test_cases)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        feature_point["test_case_run_id"] = run["run_id"]
        feature_point["test_case_count"] = len(test_cases)
        feature_point["test_cases_updated_at"] = now
        requirement["updated_at"] = now
        _save_requirements(requirements)
        return _json_response({
            "run_id": run["run_id"],
            "count": len(test_cases),
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


@app.route("/configure", methods=["GET", "POST"])
def configure():
    if request.method == "POST":
        config = {
            "provider": request.form.get("provider", "openai"),
            "base_url": request.form.get("base_url", "").strip(),
            "api_key": request.form.get("api_key", "").strip(),
            "model": request.form.get("model", "").strip(),
            "feishu_app_id": request.form.get("feishu_app_id", "").strip(),
            "feishu_app_secret": request.form.get("feishu_app_secret", "").strip(),
            "feishu_domain": request.form.get("feishu_domain", "https://open.feishu.cn").strip(),
        }
        save_config(config)
        flash("Configuration saved successfully.", "success")
        return redirect(url_for("configure"))

    config = load_config()
    return render_template(
        "configure.html",
        config=config,
        provider_defaults=PROVIDER_DEFAULTS,
    )


@app.route("/generate", methods=["POST"])
def generate():
    try:
        # --- Parse requirements document ---
        requirements_text = None

        # Option 1: Feishu document URL
        feishu_url = request.form.get("feishu_url", "").strip()
        if feishu_url:
            if is_feishu_url(feishu_url):
                try:
                    requirements_text = parse_document(feishu_url)
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

        # --- Generate test cases ---
        test_cases = generate_test_cases(requirements_text, code_structure_text)

        # --- Store in session for result/download routes ---
        run = _store_test_case_run(test_cases)
        run_id = run["run_id"]

        # --- Create Feishu document ---
        try:
            feishu_url = create_test_case_doc(test_cases, f"测试用例 - {run_id}")
            session["feishu_doc_url"] = feishu_url
        except Exception as e:
            logger.warning("Failed to create Feishu document: %s", e)
            session["feishu_doc_url"] = ""

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
    tc_json_path = session.get("tc_json_path")
    if tc_json_path and os.path.exists(tc_json_path):
        with open(tc_json_path, "r", encoding="utf-8") as f:
            test_cases = json.load(f)
    else:
        test_cases = None
    if not test_cases:
        flash("No test cases to display. Please generate first.", "warning")
        return redirect(url_for("index"))
    return render_template("results.html", test_cases=test_cases, feishu_doc_url=session.get("feishu_doc_url", ""), excel_path=session.get("excel_path", ""))


@app.route("/run-test")
def run_test():
    excel_path = session.get("excel_path", "")
    return render_template("run_test.html", excel_path=excel_path)


@app.route("/download/<fmt>")
def download(fmt: str):
    if fmt == "excel":
        path = session.get("excel_path")
        if path and os.path.exists(path):
            return send_file(path, as_attachment=True, download_name="test_cases.xlsx")
    elif fmt == "markdown":
        path = session.get("md_path")
        if path and os.path.exists(path):
            return send_file(path, as_attachment=True, download_name="test_cases.md")

    flash("File not found. Please regenerate test cases.", "warning")
    return redirect(url_for("results"))


@app.route("/api/create-feishu-doc", methods=["POST"])
def api_create_feishu_doc():
    """Create a Feishu document from the current session's test cases."""
    tc_json_path = session.get("tc_json_path")
    if not tc_json_path or not os.path.exists(tc_json_path):
        return json.dumps({"error": "没有测试用例数据"}), 400, {"Content-Type": "application/json"}
    with open(tc_json_path, "r", encoding="utf-8") as f:
        test_cases = json.load(f)
    try:
        run_id = session.get("run_id", "unknown")
        feishu_url = create_test_case_doc(test_cases, f"测试用例 - {run_id}")
        session["feishu_doc_url"] = feishu_url
        return json.dumps({"url": feishu_url}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning("Failed to create Feishu doc: %s", e)
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/generate-scripts", methods=["POST"])
def api_generate_scripts():
    """Generate executable test scripts from test cases + code paths."""
    tc_json_path = session.get("tc_json_path")
    if not tc_json_path or not os.path.exists(tc_json_path):
        return json.dumps({"error": "没有测试用例数据，请先生成测试用例"}), 400, {"Content-Type": "application/json"}

    data = request.get_json()
    code_paths = data.get("code_paths", [])
    if not code_paths:
        return json.dumps({"error": "请提供至少一个代码路径"}), 400, {"Content-Type": "application/json"}

    with open(tc_json_path, "r", encoding="utf-8") as f:
        test_cases = json.load(f)

    try:
        result = generate_test_scripts(test_cases, code_paths)
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

    tc_json_path = session.get("tc_json_path")
    if not tc_json_path or not os.path.exists(tc_json_path):
        return json.dumps({"error": "没有测试用例数据"}), 400, {"Content-Type": "application/json"}

    data = request.get_json()
    feedback = data.get("feedback", "").strip()
    if not feedback:
        return json.dumps({"error": "请输入修改描述"}), 400, {"Content-Type": "application/json"}

    with open(tc_json_path, "r", encoding="utf-8") as f:
        old_cases = json.load(f)

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

        run = _store_test_case_run(test_cases)
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
    """List previously generated test case files."""
    records = []
    if os.path.isdir(OUTPUT_DIR):
        for f in sorted(os.listdir(OUTPUT_DIR), reverse=True):
            match = re.fullmatch(r"testcases_([0-9a-fA-F]{12})\.json", f)
            if not match:
                continue
            fpath = os.path.join(OUTPUT_DIR, f)
            try:
                stat = os.stat(fpath)
                with open(fpath, "r", encoding="utf-8") as fh:
                    tc = json.load(fh)
                count = len(tc) if isinstance(tc, list) else 0
            except (OSError, json.JSONDecodeError, TypeError):
                logger.warning("Skipping unreadable history file: %s", fpath)
                continue
            records.append({
                "run_id": match.group(1),
                "count": count,
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
            })
    return render_template("history.html", records=records)


@app.route("/history/<run_id>")
def history_detail(run_id: str):
    """View a specific historical test case run."""
    if not re.fullmatch(r"[0-9a-fA-F]{12}", run_id):
        flash("记录编号无效。", "warning")
        return redirect(url_for("history"))

    tc_json_path = os.path.join(OUTPUT_DIR, f"testcases_{run_id}.json")
    if not os.path.exists(tc_json_path):
        flash("记录不存在。", "warning")
        return redirect(url_for("history"))
    try:
        with open(tc_json_path, "r", encoding="utf-8") as f:
            test_cases = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        logger.exception("Failed to read history record: %s", tc_json_path)
        flash("历史记录文件损坏或无法读取。", "danger")
        return redirect(url_for("history"))
    if not isinstance(test_cases, list) or not test_cases:
        flash("历史记录中没有有效的测试用例。", "warning")
        return redirect(url_for("history"))

    excel_path = os.path.join(OUTPUT_DIR, f"testcases_{run_id}.xlsx")
    if not os.path.exists(excel_path):
        excel_path = ""
    md_path = os.path.join(OUTPUT_DIR, f"testcases_{run_id}.md")
    if not os.path.exists(md_path):
        md_path = ""
    # Store in session so download route works
    session["run_id"] = run_id
    session["excel_path"] = excel_path
    session["md_path"] = md_path
    session["tc_json_path"] = tc_json_path
    session["feishu_doc_url"] = ""
    return render_template("results.html", test_cases=test_cases, feishu_doc_url="", excel_path=excel_path)


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
    app.run(debug=True, host="0.0.0.0", port=8899)
