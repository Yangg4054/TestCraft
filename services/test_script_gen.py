"""Generate executable test scripts with full project architecture."""

import json
import logging
import os
import re
import tempfile

from services.ai_generator import call_llm
from services.code_analyzer import analyze_code

logger = logging.getLogger(__name__)

SCRIPT_GEN_PROMPT = """You are a senior test automation architect. Given QA test cases and project source code, generate a COMPLETE, professional, executable test automation project.

## Architecture Requirements

You MUST generate a full project with these files:

### 1. conftest.py (REQUIRED)
```python
import pytest
import allure
import os
from datetime import datetime

SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = item.name
        png_path = os.path.join(SCREENSHOTS_DIR, f"{name}_{ts}.png")
        # Try to capture screenshot from driver fixture if available
        driver = item.funcargs.get("driver") or item.funcargs.get("browser")
        if driver and hasattr(driver, "save_screenshot"):
            try:
                driver.save_screenshot(png_path)
                allure.attach.file(png_path, name=f"失败截图-{name}", attachment_type=allure.attachment_type.PNG)
            except Exception:
                pass
        # Always attach the error message
        if call.excinfo:
            allure.attach(str(call.excinfo.getrepr()), name="错误详情", attachment_type=allure.attachment_type.TEXT)

def pytest_collection_modifyitems(items):
    for item in items:
        # Add allure labels from markers
        for marker in item.iter_markers("priority"):
            allure.dynamic.severity(marker.args[0] if marker.args else "normal")

@pytest.fixture(scope="session")
def base_url():
    return os.environ.get("BASE_URL", "http://localhost:8000")

@pytest.fixture(scope="session")
def api(base_url):
    from common.api_client import ApiClient
    return ApiClient(base_url)
```

### 2. pytest.ini (REQUIRED)
```ini
[pytest]
python_files = test_*.py
python_classes = Test*
python_functions = test_*
testpaths = testcases
addopts =
    -v
    -s
    --tb=short
    --alluredir=allure-results
    --clean-alluredir
markers =
    P0: 核心功能
    P1: 重要功能
    P2: 一般功能
    P3: 边缘场景
    api: 接口测试
    ui: UI测试
    smoke: 冒烟测试
```

### 3. common/api_client.py (REQUIRED for API tests)
```python
import httpx
import allure
import json

class ApiClient:
    def __init__(self, base_url: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.last_response = None

    def request(self, method, path, step_name="", **kwargs):
        url = path
        with allure.step(step_name or f"{method.upper()} {path}"):
            allure.attach(json.dumps(kwargs, ensure_ascii=False, default=str), name="请求参数", attachment_type=allure.attachment_type.JSON)
            self.last_response = self.client.request(method, url, **kwargs)
            allure.attach(
                json.dumps({"status": self.last_response.status_code, "body": self.last_response.text[:2000]}, ensure_ascii=False),
                name="响应结果", attachment_type=allure.attachment_type.JSON
            )
            return self.last_response

    def get(self, path, step_name="", **kw):
        return self.request("GET", path, step_name, **kw)

    def post(self, path, step_name="", **kw):
        return self.request("POST", path, step_name, **kw)

    def put(self, path, step_name="", **kw):
        return self.request("PUT", path, step_name, **kw)

    def delete(self, path, step_name="", **kw):
        return self.request("DELETE", path, step_name, **kw)

    def assert_status(self, expected: int):
        with allure.step(f"验证状态码为 {expected}"):
            assert self.last_response.status_code == expected, \\
                f"期望 {expected}, 实际 {self.last_response.status_code}"

    def assert_json_field(self, field: str, expected):
        with allure.step(f"验证响应字段 {field} = {expected}"):
            data = self.last_response.json()
            parts = field.split(".")
            val = data
            for p in parts:
                val = val[p] if isinstance(val, dict) else val[int(p)]
            assert val == expected, f"期望 {expected}, 实际 {val}"
```

### 4. common/base_page.py (REQUIRED for UI tests)
```python
import allure
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os
from datetime import datetime

SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "screenshots")

class BasePage:
    def __init__(self, driver, timeout=10):
        self.driver = driver
        self.wait = WebDriverWait(driver, timeout)

    def find(self, locator):
        return self.wait.until(EC.presence_of_element_located(locator))

    def click(self, locator, step=""):
        with allure.step(step or f"点击元素 {locator}"):
            el = self.wait.until(EC.element_to_be_clickable(locator))
            el.click()

    def input_text(self, locator, text, step=""):
        with allure.step(step or f"输入 '{text}'"):
            el = self.find(locator)
            el.clear()
            el.send_keys(text)

    def get_text(self, locator, step=""):
        with allure.step(step or f"获取元素文本 {locator}"):
            return self.find(locator).text

    def is_visible(self, locator, timeout=5):
        try:
            WebDriverWait(self.driver, timeout).until(EC.visibility_of_element_located(locator))
            return True
        except:
            return False

    def screenshot(self, name="screenshot"):
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOTS_DIR, f"{name}_{ts}.png")
        self.driver.save_screenshot(path)
        allure.attach.file(path, name=name, attachment_type=allure.attachment_type.PNG)
        return path
```

### 5. common/logger.py (REQUIRED)
```python
import logging
import sys

def get_logger(name="autotest"):
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        ))
        logger.addHandler(handler)
    return logger
```

## Test Script Rules

For each test case, generate a test function following this pattern:

```python
@allure.feature("模块名")
@allure.story("测试场景")
@allure.title("TC-001: 测试用例名称")
@allure.severity(allure.severity_level.CRITICAL)  # Map P0=CRITICAL, P1=NORMAL, P2=MINOR, P3=TRIVIAL
@pytest.mark.P0  # Priority marker
@pytest.mark.api  # or @pytest.mark.ui
def test_tc001_descriptive_name(api):  # or (driver) for UI
    \"\"\"
    前置条件: xxx
    测试步骤:
    1. xxx
    2. xxx
    预期结果: xxx
    \"\"\"
    with allure.step("Step 1: 具体操作描述"):
        print(">>> [TC-001] Step 1: 具体操作描述")
        # actual test code
        result = api.get("/endpoint", step_name="请求xxx接口")
        api.assert_status(200)

    with allure.step("Step 2: 验证结果"):
        print(">>> [TC-001] Step 2: 验证结果")
        api.assert_json_field("data.name", "expected_value")
```

## Critical Rules
- EVERY test step MUST use `allure.step()` AND `print(">>> [TC-ID] Step N: description")`
- EVERY test function MUST have allure decorators (feature, story, title, severity)
- EVERY test function MUST have a pytest priority marker (@pytest.mark.P0/P1/P2/P3)
- Test functions MUST use `api` fixture for API tests, `driver` fixture for UI tests
- Failed assertions should NOT block other tests — each test is independent
- Priority mapping: P0→CRITICAL, P1→NORMAL, P2→MINOR, P3→TRIVIAL
- Separate API tests and UI tests into different files (test_api_*.py, test_ui_*.py)
- Match the language of the test cases (Chinese test cases → Chinese step descriptions)
- Generate REAL, executable test code based on actual source code analysis

## Output Format
Return a JSON object:
{
  "architecture": "项目架构树文本描述",
  "scripts": [
    {
      "filename": "conftest.py",
      "language": "python",
      "content": "... complete file content ...",
      "type": "config"
    },
    {
      "filename": "pytest.ini",
      "language": "ini",
      "content": "...",
      "type": "config"
    },
    {
      "filename": "common/api_client.py",
      "language": "python",
      "content": "...",
      "type": "common"
    },
    {
      "filename": "common/base_page.py",
      "language": "python",
      "content": "...",
      "type": "common"
    },
    {
      "filename": "common/logger.py",
      "language": "python",
      "content": "...",
      "type": "common"
    },
    {
      "filename": "testcases/test_api_xxx.py",
      "language": "python",
      "content": "...",
      "type": "testcase"
    }
  ]
}

IMPORTANT: Return ONLY the JSON object, no markdown formatting."""


def generate_test_scripts(
    test_cases: list[dict],
    code_paths: list[dict],
) -> dict:
    """Generate a complete test automation project from test cases and code.

    Returns:
        dict with "architecture" and "scripts" list, each script has
        filename, language, content, path, type
    """
    # Analyze each code path
    code_summaries = []
    for cp in code_paths:
        path = cp["path"]
        label = cp.get("label", "")
        try:
            structure = analyze_code(path)
            code_summaries.append(f"## {label} ({path})\n{structure.to_text()[:8000]}")
        except Exception as e:
            logger.warning("Failed to analyze %s: %s", path, e)
            code_summaries.append(f"## {label} ({path})\nAnalysis failed: {e}")

    # Read key source files for more context
    source_snippets = []
    for cp in code_paths:
        snippets = _read_key_files(cp["path"])
        if snippets:
            source_snippets.append(f"## Source: {cp.get('label', '')} ({cp['path']})\n{snippets}")

    # Categorize test cases
    api_cases = [tc for tc in test_cases if tc.get("type", "").lower() in ("functional", "integration", "api", "performance", "security")]
    ui_cases = [tc for tc in test_cases if tc.get("type", "").lower() in ("ui", "e2e")]
    other_cases = [tc for tc in test_cases if tc not in api_cases and tc not in ui_cases]
    # If no clear categorization, treat all as API tests
    if not api_cases and not ui_cases:
        api_cases = test_cases

    tc_text = json.dumps(test_cases[:40], ensure_ascii=False, indent=2)

    test_type_info = f"""
## 测试用例分类
- 接口测试用例: {len(api_cases)} 个
- UI测试用例: {len(ui_cases)} 个
- 其他测试用例: {len(other_cases)} 个 (归入接口测试)
"""

    user_content = f"""## Test Cases (共 {len(test_cases)} 个)
{tc_text}

{test_type_info}

## Code Structure
{chr(10).join(code_summaries)}

## Key Source Files
{chr(10).join(source_snippets)[:20000]}

Generate a COMPLETE test automation project with full architecture.
You MUST include: conftest.py, pytest.ini, common/api_client.py, common/logger.py, and all testcase files.
If there are UI test cases, also include common/base_page.py and pages/*.
Return ONLY a JSON object with "architecture" and "scripts" array."""

    content = call_llm(SCRIPT_GEN_PROMPT, user_content)
    return _parse_project(content)


def _read_key_files(path: str, max_total: int = 10000) -> str:
    """Read key source files from a path for context."""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:max_total]
        except Exception:
            return ""

    if not os.path.isdir(path):
        return ""

    key_patterns = [
        "app.py", "main.py", "server.py", "index.py",
        "routes.py", "views.py", "api.py", "handlers.py",
        "index.js", "index.ts", "app.js", "app.ts",
        "main.go", "main.rs",
    ]
    parts = []
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (
            "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build"
        )]
        for fname in files:
            if total >= max_total:
                break
            if fname in key_patterns or fname.endswith((".py", ".js", ".ts", ".go")):
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, path)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()[:3000]
                    parts.append(f"### {rel}\n```\n{content}\n```")
                    total += len(content)
                except Exception:
                    continue
    return "\n".join(parts)


def _parse_project(content: str) -> dict:
    """Parse LLM response into a project structure with scripts."""
    content = content.strip()
    content = re.sub(r"```(?:json)?\s*\n?", "", content).strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                # Try truncated JSON recovery
                fragment = match.group()
                last_brace = fragment.rfind("}")
                if last_brace > 0:
                    try:
                        parsed = json.loads(fragment[:last_brace + 1])
                    except json.JSONDecodeError:
                        raise ValueError("Failed to parse test script response as JSON.")
                else:
                    raise ValueError("Failed to parse test script response as JSON.")
        else:
            raise ValueError("Failed to parse test script response as JSON.")

    scripts = parsed.get("scripts", [])
    if not scripts:
        raise ValueError("No test scripts generated.")

    architecture = parsed.get("architecture", "")

    # Save scripts to persistent local directory
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "test_projects")
    os.makedirs(base_dir, exist_ok=True)
    output_dir = os.path.join(base_dir, f"project_{ts}")
    os.makedirs(output_dir, exist_ok=True)
    results = []

    for s in scripts:
        filename = s.get("filename", "test_generated.py")
        script_content = s.get("content", "")
        language = s.get("language", "python")
        script_type = s.get("type", "testcase")

        # Create subdirectories as needed
        filepath = os.path.join(output_dir, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(script_content)

        results.append({
            "filename": filename,
            "language": language,
            "content": script_content,
            "path": filepath,
            "type": script_type,
        })

    # Create screenshots directory
    os.makedirs(os.path.join(output_dir, "screenshots"), exist_ok=True)

    # Generate architecture text if not provided by LLM
    if not architecture:
        architecture = _build_architecture_text(results)

    logger.info("Generated %d files in project: %s", len(results), output_dir)
    return {
        "architecture": architecture,
        "scripts": results,
        "project_dir": output_dir,
    }


def _build_architecture_text(scripts: list[dict]) -> str:
    """Build a tree-like architecture text from script list."""
    lines = ["generated_tests/"]
    dirs_seen = set()
    # Sort: config first, then common, then testcases, then pages
    type_order = {"config": 0, "common": 1, "testcase": 2, "page": 3}
    sorted_scripts = sorted(scripts, key=lambda s: (type_order.get(s.get("type", ""), 9), s["filename"]))

    for s in sorted_scripts:
        parts = s["filename"].split("/")
        if len(parts) > 1:
            dir_name = parts[0]
            if dir_name not in dirs_seen:
                dirs_seen.add(dir_name)
                lines.append(f"├── {dir_name}/")
            lines.append(f"│   └── {parts[-1]}")
        else:
            lines.append(f"├── {parts[0]}")

    lines.append("└── screenshots/")
    return "\n".join(lines)
