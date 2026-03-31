"""AI-powered test case generator using configurable LLM providers."""

import json
import logging
import re
import httpx

from config import load_config

logger = logging.getLogger(__name__)


def call_llm(system_prompt: str, user_content: str) -> str:
    """Call the configured LLM and return the raw text response."""
    config = load_config()
    if not config.get("api_key"):
        raise ValueError("LLM API key not configured.")

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
    data = resp.json()
    return data["choices"][0]["message"]["content"]


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

SYSTEM_PROMPT = """You are a senior QA engineer. Generate comprehensive test cases from requirements and code, achieving 100% coverage.

## Testing Methods (apply all applicable)
1. 等价类测试法: Valid/invalid equivalence classes
2. 边界值测试法: Test at min, min+1, max-1, max, below min, above max
3. 因果图法: Map input conditions to output actions
4. 判定表法: Enumerate condition combinations for complex rules
5. 正交排列法: Cover parameter combinations efficiently
6. 错误推算法: Null, empty, special chars, concurrency, extreme values
7. 场景法: Main flow, alternate flows, exception flows

## Output: JSON array of objects
{"id":"TC-001","module":"...","name":"...","priority":"P0|P1|P2|P3","preconditions":"...","steps":"detailed steps with specific values","expected_result":"precise outcome","type":"Functional|UI|Edge Case|Performance|Security|Integration","method":"等价类|边界值|因果图|判定表|正交排列|错误推算|场景法"}

## Rules
- P0=critical, P1=important, P2=secondary, P3=edge cases
- Steps: specific input values, click targets, API params
- Expected results: specific output values, status codes, UI states
- Generate 30-60 test cases. Match language of requirements.
- Cover: happy path, edge cases, error handling, boundary values, negative cases

Return ONLY the JSON array."""


def generate_test_cases(
    requirements_text: str,
    code_structure_text: str | None = None,
) -> list[dict]:
    """Generate test cases using the configured LLM provider."""
    user_content = _build_user_prompt(requirements_text, code_structure_text)
    content = call_llm(SYSTEM_PROMPT, user_content)
    return _parse_response(content)


def _build_user_prompt(requirements_text: str, code_structure_text: str | None) -> str:
    parts = ["## Requirements Document\n", requirements_text[:10000]]
    if code_structure_text:
        parts.append("\n\n## Code Structure\n")
        parts.append(code_structure_text[:8000])
    parts.append(
        "\n\nBased on the above, generate comprehensive test cases. "
        "Return ONLY a JSON array of test case objects."
    )
    return "\n".join(parts)


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
