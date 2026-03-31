"""Create Feishu documents from generated test cases."""

import logging

import requests

from services.doc_parser import get_feishu_config, get_tenant_access_token

logger = logging.getLogger(__name__)


def _feishu_api_post(path: str, body: dict) -> dict:
    """Make an authenticated POST request to Feishu Open API."""
    _, _, domain = get_feishu_config()
    token = get_tenant_access_token()
    url = f"{domain}/open-apis{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(f"Feishu API error: {data.get('msg', 'unknown')} (code={data.get('code')})")
    return data.get("data", {})


def _feishu_api_patch(path: str, body: dict) -> dict:
    """Make an authenticated PATCH request to Feishu Open API."""
    _, _, domain = get_feishu_config()
    token = get_tenant_access_token()
    url = f"{domain}/open-apis{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.patch(url, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(f"Feishu API error: {data.get('msg', 'unknown')} (code={data.get('code')})")
    return data.get("data", {})


def _text_el(text: str) -> dict:
    return {"text_run": {"content": text}}


def _bold_el(text: str) -> dict:
    return {"text_run": {"content": text, "text_element_style": {"bold": True}}}


def _make_text_block(elements: list[dict]) -> dict:
    return {"block_type": 2, "text": {"elements": elements}}


def _make_heading(level: int, text: str) -> dict:
    key = f"heading{level}"
    return {"block_type": level + 2, key: {"elements": [_text_el(text)]}}


def _add_children(document_id: str, parent_id: str, blocks: list[dict]):
    """Add children blocks in batches."""
    # Feishu API allows up to 50 children per request
    batch_size = 50
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i:i + batch_size]
        _feishu_api_post(
            f"/docx/v1/documents/{document_id}/blocks/{parent_id}/children",
            {"children": batch},
        )


def create_test_case_doc(test_cases: list[dict], title: str = "测试用例") -> str:
    """Create a Feishu document with test cases and return the document URL."""
    # Step 1: Create empty document
    create_data = _feishu_api_post("/docx/v1/documents", {"title": title})
    document = create_data.get("document", {})
    document_id = document.get("document_id")
    if not document_id:
        raise ValueError("Failed to create Feishu document: no document_id returned")

    logger.info("Created Feishu document: %s", document_id)
    root_block_id = document_id

    # Step 2: Build summary
    priority_counts = {}
    method_counts = {}
    for tc in test_cases:
        p = tc.get("priority", "P2")
        priority_counts[p] = priority_counts.get(p, 0) + 1
        m = tc.get("method", "")
        if m:
            method_counts[m] = method_counts.get(m, 0) + 1

    blocks = []

    # Summary
    summary_parts = [f"{k}: {v}" for k, v in sorted(priority_counts.items())]
    blocks.append(_make_text_block([
        _bold_el(f"共 {len(test_cases)} 条测试用例"),
        _text_el(f"  |  {' | '.join(summary_parts)}"),
    ]))

    if method_counts:
        method_parts = [f"{k}: {v}" for k, v in sorted(method_counts.items())]
        blocks.append(_make_text_block([
            _bold_el("测试方法覆盖: "),
            _text_el(" | ".join(method_parts)),
        ]))

    # Divider
    blocks.append({"block_type": 22, "divider": {}})

    # Add summary blocks first
    try:
        _add_children(document_id, root_block_id, blocks)
    except Exception as e:
        logger.warning("Failed to add summary: %s", e)

    # Step 3: Add test cases in batches
    for tc in test_cases:
        tc_blocks = []

        # Heading: TC-001 - 用例名称
        tc_id = tc.get("id", "")
        tc_name = tc.get("name", "")
        tc_blocks.append(_make_heading(3, f"{tc_id} — {tc_name}"))

        # Details as text blocks
        fields = [
            ("模块", tc.get("module", "")),
            ("优先级", tc.get("priority", "")),
            ("类型", tc.get("type", "")),
            ("测试方法", tc.get("method", "")),
            ("前置条件", tc.get("preconditions", "")),
            ("测试步骤", tc.get("steps", "")),
            ("预期结果", tc.get("expected_result", "")),
        ]
        for label, value in fields:
            if not value:
                continue
            tc_blocks.append(_make_text_block([
                _bold_el(f"{label}: "),
                _text_el(str(value)),
            ]))

        try:
            _add_children(document_id, root_block_id, tc_blocks)
        except Exception as e:
            logger.warning("Failed to add test case %s: %s", tc_id, e)

    # Set document to public readable
    try:
        _feishu_api_patch(
            f"/drive/v1/permissions/{document_id}/public?type=docx",
            {
                "external_access_entity": "open",
                "security_entity": "anyone_can_view",
                "comment_entity": "anyone_can_view",
                "share_entity": "anyone",
                "link_share_entity": "anyone_readable",
            },
        )
        logger.info("Set document %s to public readable", document_id)
    except Exception as e:
        logger.warning("Failed to set public permission: %s", e)

    # Grant full_access to owner
    try:
        _feishu_api_post(
            f"/drive/v1/permissions/{document_id}/members?type=docx",
            {
                "member_type": "phone",
                "member_id": "15178642013",
                "perm": "full_access",
            },
        )
        logger.info("Granted full_access to owner")
    except Exception as e:
        logger.warning("Failed to grant owner permission: %s", e)

    doc_url = f"https://presence.feishu.cn/docx/{document_id}"
    logger.info("Feishu document created: %s", doc_url)
    return doc_url
