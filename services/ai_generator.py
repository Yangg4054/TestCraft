"""AI-powered test case generator using configurable LLM providers."""

import json
import logging
import re
import httpx

from config import load_config

logger = logging.getLogger(__name__)

TARGET_CASE_COUNT = 30
MIN_CASE_COUNT = 12

FEATURE_SPLIT_PROMPT = """你是一名资深产品测试分析师。请将当前需求拆分为原子化、可独立评审、开发和测试的功能点，并与历史需求进行依赖分析。

拆分规则：
1. 覆盖所有角色、触发条件、前置条件、业务规则、状态、主流程步骤、异常流程、边界条件、权限、外部依赖和数据变化。
2. 每个功能点只描述一个可独立验证的行为或规则；复杂流程必须继续拆成更细的操作、校验、状态变化或系统响应，不能把整段需求换一种说法。
3. 验收标准必须可执行、结果明确，避免“功能正常”“符合预期”等模糊表述。
4. 对需求明确暗示的异常、权限和边界场景补充功能点，但不要凭空发明业务规则；不确定内容写入 risks。
5. 将当前需求与历史需求逐条比较。只有当前功能确实需要历史需求提供的能力、数据、流程或接口才能成立时，才记录 requirement_dependencies。
6. 主题相似、属于同一业务模块、可复用但非必需，都不算依赖。依赖项必须引用历史需求的 requirement_id 和 requirement_title，并解释原因。
7. dependencies 只记录当前需求内部或外部系统依赖；跨需求依赖统一写入 requirement_dependencies。

只返回 JSON 对象，不要返回 Markdown：
{"feature_points":[{"id":"FP-001","name":"原子功能点名称","description":"功能行为和目标","actors":["参与角色"],"trigger":"触发条件","preconditions":["前置条件"],"main_flow":["步骤1","步骤2"],"exception_flows":["异常条件及系统处理"],"business_rules":["业务规则"],"data_changes":["数据或状态变化"],"acceptance_criteria":["可验证的验收标准1","可验证的验收标准2"],"priority":"P0|P1|P2|P3","dependencies":["当前需求内部或外部系统依赖"],"requirement_dependencies":[{"requirement_id":"历史需求ID","requirement_title":"历史需求标题","dependency_type":"前置能力|数据依赖|流程依赖|接口依赖","reason":"不可缺少的具体原因"}],"risks":["风险或待确认项"]}]}
"""


def call_llm(system_prompt: str, user_content: str) -> str:
    """Call the configured LLM and return the raw text response."""
    config = load_config()
    if not config.get("api_key"):
        raise ValueError("尚未配置大模型 API Key，请先打开配置页填写。")
    if not config.get("base_url", "").strip():
        raise ValueError("尚未配置大模型 Base URL，请先打开配置页填写。")
    if not config.get("model", "").strip():
        raise ValueError("尚未配置大模型名称，请先打开配置页填写模型。")

    provider = config.get("provider", "openai")
    if provider == "anthropic":
        return _call_anthropic_raw(config, system_prompt, user_content)
    else:
        return _call_openai_raw(config, system_prompt, user_content)


def _call_openai_raw(config: dict, system_prompt: str, user_content: str) -> str:
    base_url = config.get("base_url", "https://api.openai.com/v1").rstrip("/")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.get("model", "gpt-4o"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    timeout = httpx.Timeout(connect=30, read=600, write=30, pool=30)
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        preview = resp.text.strip().replace("\n", " ")[:200]
        raise ValueError(
            f"大模型接口返回了无效 JSON（HTTP {resp.status_code}）。"
            f"请检查 Base URL 是否包含正确的 /v1 路径。响应片段：{preview or '[空响应]'}"
        ) from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("大模型接口响应缺少 choices[0].message.content 字段。") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("大模型接口返回了空内容，请检查模型名称和接口兼容性。")
    return content


def split_requirement(
    title: str,
    content: str,
    previous_requirements: list[dict] | None = None,
) -> list[dict]:
    """Split one requirement into normalized, independently testable features."""
    if not title.strip() or not content.strip():
        raise ValueError("需求标题和需求内容不能为空。")
    history_context = _build_requirement_history_context(previous_requirements or [])
    user_content = (
        f"## 需求标题\n{title.strip()}\n\n"
        f"## 完整需求内容\n{content.strip()[:30000]}\n\n"
        f"## 历史需求上下文\n{history_context}\n\n"
        "请先建立需求覆盖清单并在内部逐项核对，再按规则拆成功能点。"
        "通常至少输出 8 个原子功能点；若需求确实简单可按实际拆分，但不得合并不同角色、规则、状态或异常处理。"
    )
    parsed = _parse_feature_points(call_llm(FEATURE_SPLIT_PROMPT, user_content))
    for index, point in enumerate(parsed, 1):
        point["id"] = f"FP-{index:03d}"
    return parsed


def _build_requirement_history_context(requirements: list[dict]) -> str:
    """Build a bounded history summary suitable for dependency comparison."""
    if not requirements:
        return "暂无历史需求。跨需求依赖必须返回空数组。"

    summaries = []
    for requirement in requirements[-15:]:
        if not isinstance(requirement, dict):
            continue
        feature_summaries = []
        for point in requirement.get("feature_points", [])[:12]:
            if not isinstance(point, dict):
                continue
            name = str(point.get("name", "")).strip()
            description = str(point.get("description", "")).strip()
            if name or description:
                feature_summaries.append(f"- {name}: {description[:240]}")
        summary = (
            f"### [{requirement.get('id', '')}] {requirement.get('title', '')}\n"
            f"状态：{requirement.get('status', '')}\n"
            f"内容摘要：{str(requirement.get('content', '')).strip()[:900]}\n"
            f"已拆功能点：\n{chr(10).join(feature_summaries) if feature_summaries else '- 暂无'}"
        )
        summaries.append(summary)
    return "\n\n".join(summaries)[:20000] or "暂无有效历史需求。"


def _call_anthropic_raw(config: dict, system_prompt: str, user_content: str) -> str:
    base_url = config.get("base_url", "https://api.anthropic.com").rstrip("/")
    url = f"{base_url}/v1/messages"
    headers = {
        "x-api-key": config["api_key"],
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.get("model", "claude-sonnet-4-20250514"),
        "max_tokens": 16384,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": 0.3,
        "stream": True,
    }
    logger.info("Calling Anthropic API: %s model=%s", url, payload["model"])
    timeout = httpx.Timeout(connect=30, read=600, write=30, pool=30)

    max_retries = 2
    raw_bytes = b""
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        error_body = b"".join(resp.iter_bytes()).decode("utf-8", errors="replace")
                        error_match = re.search(r'"error"\s*:\s*"([^"]+)"', error_body)
                        msg = error_match.group(1) if error_match else error_body[:300]
                        raise ValueError(f"LLM API error ({resp.status_code}): {msg}")
                    for chunk in resp.iter_bytes():
                        raw_bytes += chunk
            break
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
            logger.warning("Anthropic API attempt %d failed: %s", attempt + 1, e)
            if attempt < max_retries:
                import time
                time.sleep(3)
                raw_bytes = b""
            else:
                raise ValueError(f"LLM API failed after {max_retries + 1} attempts: {e}")

    raw_text = raw_bytes.decode("utf-8", errors="replace")
    content_parts = []
    for line in raw_text.splitlines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        evt_type = event.get("type", "")
        if evt_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                content_parts.append(delta.get("text", ""))
        elif evt_type == "message_stop":
            break

    content = "".join(content_parts)
    if not content.strip():
        # Check if the raw response contains an error message
        error_match = re.search(r'"error"\s*:\s*"([^"]+)"', raw_text)
        if error_match:
            raise ValueError(f"LLM API error: {error_match.group(1)}")
        raise ValueError("LLM returned empty response.")
    return content

SYSTEM_PROMPT = """You are a senior QA engineer. Generate comprehensive test cases from the complete requirements document and code, achieving full requirement coverage.

## Testing Methods (apply all applicable)
1. 等价类测试法: Valid/invalid equivalence classes
2. 边界值测试法: Test at min, min+1, max-1, max, below min, above max
3. 因果图法: Map input conditions to output actions
4. 判定表法: Enumerate condition combinations for complex rules
5. 正交排列法: Cover parameter combinations efficiently
6. 错误推算法: Null, empty, special chars, concurrency, extreme values
7. 场景法: Main flow, alternate flows, exception flows

## Output: JSON object with a test_cases array (the wrapper is mandatory)
{"test_cases":[{"id":"TC-001","module":"...","name":"...","priority":"P0|P1|P2|P3","preconditions":"...","steps":"detailed steps with specific values","expected_result":"precise outcome","type":"Functional|UI|Edge Case|Performance|Security|Integration","method":"等价类|边界值|因果图|判定表|正交排列|错误推算|场景法"}]}

## Rules
- P0=critical, P1=important, P2=secondary, P3=edge cases
- Steps: specific input values, click targets, API params
- Expected results: specific output values, status codes, UI states
- First enumerate every independent requirement, rule, role, state, integration, and user flow in the document.
- Generate at least 30 test cases (and no fewer than 12 even for a small document). For every requirement, cover the happy path, validation/negative path, boundary or state transition, and permission/error behavior when applicable.
- Never return a single representative case when multiple requirements or scenarios exist.
- Match the language of the requirements.
- Cover: happy path, edge cases, error handling, boundary values, negative cases

Return ONLY the JSON object wrapper. Do not return a single test case object."""


def generate_test_cases(
    requirements_text: str,
    code_structure_text: str | None = None,
    target_count: int = TARGET_CASE_COUNT,
    min_count: int = MIN_CASE_COUNT,
) -> list[dict]:
    """Generate test cases using the configured LLM provider."""
    target_count = max(1, int(target_count))
    min_count = max(1, min(int(min_count), target_count))
    system_prompt = _test_case_system_prompt(target_count, min_count)
    coverage_prompt = _coverage_system_prompt(min_count)
    user_content = _build_user_prompt(requirements_text, code_structure_text, target_count)
    content = call_llm(system_prompt, user_content)
    initial_cases = _parse_response(content)

    # Some compatible APIs ignore the JSON wrapper and return one case. Ask for
    # a focused coverage pass so the result is useful instead of silently
    # accepting an under-sized suite.
    if len(initial_cases) < min_count:
        logger.warning(
            "Model returned only %d test cases; requesting a requirement coverage pass",
            len(initial_cases),
        )
        expansion_content = _build_expansion_prompt(
            requirements_text, code_structure_text, initial_cases
        )
        expansion = _parse_response(call_llm(coverage_prompt, expansion_content))
        merged = _merge_test_cases(initial_cases, expansion)
        if len(merged) < min_count:
            logger.warning(
                "Coverage pass returned only %d test cases; retrying missing-requirement review",
                len(merged),
            )
            retry_content = _build_expansion_prompt(
                requirements_text, code_structure_text, merged
            )
            retry = _parse_response(call_llm(coverage_prompt, retry_content))
            merged = _merge_test_cases(merged, retry)
        return merged

    return _renumber_test_cases(initial_cases)


def _test_case_system_prompt(target_count: int, min_count: int) -> str:
    return SYSTEM_PROMPT.replace(
        "Generate at least 30 test cases (and no fewer than 12 even for a small document).",
        f"Generate about {target_count} test cases (and no fewer than {min_count}).",
    )


def _coverage_system_prompt(min_count: int) -> str:
    return COVERAGE_SYSTEM_PROMPT.replace(
        "with at least 12 additional cases when gaps exist",
        f"with enough additional cases to reach at least {min_count} total cases",
    )


def _build_user_prompt(
    requirements_text: str,
    code_structure_text: str | None,
    target_count: int = TARGET_CASE_COUNT,
) -> str:
    parts = ["## Complete Requirements Document\n", requirements_text[:30000]]
    if code_structure_text:
        parts.append("\n\n## Code Structure\n")
        parts.append(code_structure_text[:12000])
    parts.append(
        "\n\nBased on the complete document, first identify every requirement and then "
        f"generate about {target_count} comprehensive test cases. "
        "Return ONLY the mandatory JSON object wrapper with a test_cases array."
    )
    return "\n".join(parts)


COVERAGE_SYSTEM_PROMPT = """You are a senior QA test design reviewer completing an existing test suite.
Read the complete requirements and existing cases. Identify requirements, business rules,
roles, states, integrations, and scenarios that are missing or under-tested. Generate
additional concrete test cases only. Include happy path, negative, boundary, permission,
error handling, and state-transition cases where applicable.

Return ONLY this JSON object shape, with at least 12 additional cases when gaps exist:
{"test_cases":[{"id":"TC-001","module":"...","name":"...","priority":"P0|P1|P2|P3","preconditions":"...","steps":"...","expected_result":"...","type":"Functional|UI|Edge Case|Performance|Security|Integration","method":"等价类|边界值|因果图|判定表|正交排列|错误推算|场景法"}]}
Do not return a single object and do not repeat existing cases."""


def _build_expansion_prompt(
    requirements_text: str,
    code_structure_text: str | None,
    existing_cases: list[dict],
) -> str:
    parts = ["## Complete Requirements Document\n", requirements_text[:30000]]
    if code_structure_text:
        parts.extend(["\n\n## Code Structure\n", code_structure_text[:12000]])
    parts.extend([
        "\n\n## Existing Test Cases\n",
        json.dumps(existing_cases, ensure_ascii=False, indent=2)[:12000],
        "\n\nGenerate additional cases for every uncovered requirement and scenario. "
        "Return only the JSON object wrapper.",
    ])
    return "".join(parts)


def _merge_test_cases(*groups: list[dict]) -> list[dict]:
    """Merge generated groups while removing duplicate module/name pairs."""
    merged = []
    seen = set()
    for group in groups:
        for case in group:
            key = (
                str(case.get("module", "")).strip().casefold(),
                str(case.get("name", "")).strip().casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(case)
    return _renumber_test_cases(merged)


def _renumber_test_cases(cases: list[dict]) -> list[dict]:
    """Give the final suite stable sequential IDs after merging model output."""
    for index, case in enumerate(cases, 1):
        case["id"] = f"TC-{index:03d}"
    return cases


def _parse_response(content: str) -> list[dict]:
    """Parse LLM response into a list of test case dicts."""
    logger.info("Raw LLM response length: %d chars", len(content))
    logger.info("Raw LLM response first 1000 chars: %s", content[:1000])
    logger.info("Raw LLM response last 500 chars: %s", content[-500:] if len(content) > 500 else content)
    content = content.strip()

    # Strip markdown code fences if present
    content = re.sub(r"```(?:json)?\s*\n?", "", content)
    content = content.strip()

    # Try direct JSON parse
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return _validate_test_cases(parsed)
        if isinstance(parsed, dict):
            # Some models wrap in {"test_cases": [...]}
            for key in ("test_cases", "testCases", "tests", "data"):
                if key in parsed and isinstance(parsed[key], list):
                    return _validate_test_cases(parsed[key])
            return _validate_test_cases([parsed])
    except json.JSONDecodeError:
        pass

    # Try to extract JSON array from the text using bracket matching
    start = content.find("[")
    if start != -1:
        # Find the matching closing bracket
        depth = 0
        end = -1
        for i in range(start, len(content)):
            if content[i] == "[":
                depth += 1
            elif content[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end != -1:
            try:
                parsed = json.loads(content[start:end + 1])
                return _validate_test_cases(parsed)
            except json.JSONDecodeError:
                pass

        # JSON may be truncated (max_tokens hit) — try to salvage
        fragment = content[start:]
        # Close any unclosed strings/objects/arrays to recover partial results
        fragment = _try_fix_truncated_json(fragment)
        if fragment:
            try:
                parsed = json.loads(fragment)
                if isinstance(parsed, list):
                    logger.warning("Recovered %d test cases from truncated response", len(parsed))
                    return _validate_test_cases(parsed)
            except json.JSONDecodeError:
                pass

    raise ValueError(
        "Failed to parse LLM response as JSON. "
        "The AI returned an unexpected format. Please try regenerating."
    )


def _parse_feature_points(content: str) -> list[dict]:
    """Parse and normalize the feature-point response from an LLM."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("AI 未返回功能点内容，请重试。")
    text = re.sub(r"```(?:json)?\s*\n?", "", content).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("AI 返回的功能点不是有效 JSON，请重试。")
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("AI 返回的功能点不是有效 JSON，请重试。") from exc

    if isinstance(parsed, dict):
        points = parsed.get("feature_points") or parsed.get("featurePoints") or parsed.get("features")
        if points is None and all(key in parsed for key in ("name", "description")):
            points = [parsed]
    elif isinstance(parsed, list):
        points = parsed
    else:
        points = None
    if not isinstance(points, list):
        raise ValueError("AI 返回中缺少 feature_points 数组，请重试。")

    normalized = []
    for index, point in enumerate(points, 1):
        if not isinstance(point, dict):
            continue
        criteria = point.get("acceptance_criteria", point.get("acceptanceCriteria", []))
        dependencies = point.get("dependencies", point.get("depends_on", []))
        risks = point.get("risks", point.get("risk", []))
        requirement_dependencies = _normalize_requirement_dependencies(
            point.get("requirement_dependencies", point.get("requirementDependencies", []))
        )
        normalized.append({
            "id": point.get("id", f"FP-{index:03d}"),
            "name": str(point.get("name", point.get("title", f"功能点 {index}"))).strip(),
            "description": str(point.get("description", point.get("detail", ""))).strip(),
            "actors": _as_string_list(point.get("actors", point.get("roles", []))),
            "trigger": str(point.get("trigger", "")).strip(),
            "preconditions": _as_string_list(point.get("preconditions", [])),
            "main_flow": _as_string_list(point.get("main_flow", point.get("mainFlow", []))),
            "exception_flows": _as_string_list(
                point.get("exception_flows", point.get("exceptionFlows", []))
            ),
            "business_rules": _as_string_list(
                point.get("business_rules", point.get("businessRules", []))
            ),
            "data_changes": _as_string_list(
                point.get("data_changes", point.get("dataChanges", []))
            ),
            "acceptance_criteria": _as_string_list(criteria),
            "priority": _normalize_priority(point.get("priority", "P1")),
            "dependencies": _as_string_list(dependencies),
            "requirement_dependencies": requirement_dependencies,
            "risks": _as_string_list(risks),
        })
    if not normalized:
        raise ValueError("AI 未生成有效功能点，请重试。")
    return normalized


def _normalize_requirement_dependencies(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            continue
        requirement_id = str(
            item.get("requirement_id", item.get("requirementId", ""))
        ).strip()
        requirement_title = str(
            item.get("requirement_title", item.get("requirementTitle", ""))
        ).strip()
        reason = str(item.get("reason", "")).strip()
        if not requirement_id and not requirement_title:
            continue
        normalized.append({
            "requirement_id": requirement_id,
            "requirement_title": requirement_title,
            "dependency_type": str(
                item.get("dependency_type", item.get("dependencyType", "依赖"))
            ).strip() or "依赖",
            "reason": reason,
        })
    return normalized


def _as_string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    value = str(value).strip()
    return [value] if value else []


def _normalize_priority(value: str) -> str:
    priority = str(value or "P1").upper().strip()
    return priority if priority in {"P0", "P1", "P2", "P3"} else "P1"


def _try_fix_truncated_json(fragment: str) -> str | None:
    """Attempt to fix a truncated JSON array by closing open brackets."""
    # Find the last complete object (ends with })
    last_complete = fragment.rfind("}")
    if last_complete == -1:
        return None
    # Trim to last complete object, close the array
    trimmed = fragment[:last_complete + 1].rstrip().rstrip(",") + "]"
    return trimmed


REQUIRED_FIELDS = {"id", "module", "name", "priority", "preconditions", "steps", "expected_result", "type"}


def _validate_test_cases(cases: list) -> list[dict]:
    """Validate and normalize test case objects."""
    valid = []
    for i, tc in enumerate(cases):
        if not isinstance(tc, dict):
            continue
        # Ensure all required fields exist with defaults
        normalized = {
            "id": tc.get("id", f"TC-{i+1:03d}"),
            "module": tc.get("module", "General"),
            "name": tc.get("name", tc.get("test_case_name", f"Test Case {i+1}")),
            "priority": tc.get("priority", "P2"),
            "preconditions": tc.get("preconditions", tc.get("precondition", "N/A")),
            "steps": tc.get("steps", tc.get("test_steps", "N/A")),
            "expected_result": tc.get("expected_result", tc.get("expectedResult", "N/A")),
            "type": tc.get("type", tc.get("category", "Functional")),
            "method": tc.get("method", ""),
        }
        # Normalize priority
        p = normalized["priority"].upper()
        if p not in ("P0", "P1", "P2", "P3"):
            normalized["priority"] = "P2"
        else:
            normalized["priority"] = p
        valid.append(normalized)

    if not valid:
        raise ValueError("No valid test cases found in AI response.")

    logger.info("Generated %d test cases", len(valid))
    return valid
