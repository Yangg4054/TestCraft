"""应用配置（大模型 + 飞书凭证）的读写。

配置来源按优先级从低到高：
    1. DEFAULT_CONFIG      内置默认值
    2. config.json         本地文件，仅用于开发；容器里会随重启丢失
    3. app_config 表       配置了数据库时的主存储，重启 / 重建 / 扩缩容都不丢
    4. 环境变量            最高优先级，供 k8s 从 Secret 注入，页面上只读展示

以前配置只写在 config.json，而该文件位于应用目录（容器内即镜像文件系统），
每次重新部署或 Pod 重建都会被还原，导致"配置又失效了"。
"""

import json
import logging
import os
import time

import db

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "feishu_domain": "https://open.feishu.cn",
}

# 不应回显到页面、日志里也要打码的字段
SECRET_FIELDS = ("api_key", "feishu_app_secret")

# 环境变量优先，方便 k8s 用 Secret 注入而不把凭证写进数据库
ENV_OVERRIDES = {
    "provider": "TESTCRAFT_LLM_PROVIDER",
    "base_url": "TESTCRAFT_LLM_BASE_URL",
    "api_key": "TESTCRAFT_LLM_API_KEY",
    "model": "TESTCRAFT_LLM_MODEL",
    "feishu_app_id": "TESTCRAFT_FEISHU_APP_ID",
    "feishu_app_secret": "TESTCRAFT_FEISHU_APP_SECRET",
    "feishu_domain": "TESTCRAFT_FEISHU_DOMAIN",
}

PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
    },
    "custom": {
        "base_url": "",
        "model": "",
    },
}

# 数据库读取加一个短缓存：生成用例时每次调用都会 load_config()，
# 10 秒 TTL 既避免频繁往返，也能让其他副本改动很快生效。
_CACHE_TTL_SECONDS = 10
_cache: dict | None = None
_cache_at: float = 0.0


def uses_database() -> bool:
    """True 表示配置存在数据库里（重启不丢）。"""
    return db.is_enabled()


def env_locked_fields() -> list[str]:
    """由环境变量提供、页面上不可编辑的字段。"""
    return [key for key, env in ENV_OVERRIDES.items() if os.environ.get(env, "").strip()]


def _read_file_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load config file: %s", e)
        return {}


def _write_file_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_config(use_cache: bool = True) -> dict:
    """Merge every configuration source in priority order."""
    global _cache, _cache_at
    if use_cache and _cache is not None and (time.time() - _cache_at) < _CACHE_TTL_SECONDS:
        return dict(_cache)

    config = dict(DEFAULT_CONFIG)
    config.update({
        key: value for key, value in _read_file_config().items()
        if key in DEFAULT_CONFIG and value not in (None, "")
    })

    if uses_database():
        try:
            stored = db.load_app_config()
            config.update({
                key: value for key, value in stored.items()
                if key in DEFAULT_CONFIG and value not in (None, "")
            })
        except Exception:
            # 数据库暂时不可用时退回文件 / 默认值，不能让整个页面挂掉。
            logger.exception("Failed to load config from database")

    for key, env_name in ENV_OVERRIDES.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            config[key] = value

    _cache, _cache_at = dict(config), time.time()
    return config


def save_config(config: dict) -> None:
    """Persist config to the database when available, else to the local file."""
    current = load_config(use_cache=False)
    locked = set(env_locked_fields())
    safe = {}
    for key in DEFAULT_CONFIG:
        if key in locked:
            # 环境变量提供的值不写回存储，避免页面上的旧值覆盖 Secret。
            continue
        value = config.get(key)
        if value is None:
            value = current.get(key, DEFAULT_CONFIG[key])
        safe[key] = str(value)

    if uses_database():
        db.save_app_config(safe, updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("Config saved to database (%s)", ", ".join(sorted(safe)))
    else:
        merged = {**{k: current.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}, **safe}
        _write_file_config(merged)
        logger.warning(
            "Config saved to %s — 未配置 TESTCRAFT_DATABASE_URL，容器重启后该文件会丢失",
            CONFIG_FILE,
        )

    invalidate_cache()


def invalidate_cache() -> None:
    global _cache, _cache_at
    _cache, _cache_at = None, 0.0


def mask_secret(value: str) -> str:
    """Render a stored secret as a hint instead of echoing it into the page."""
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"
