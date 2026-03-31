import json
import os
import logging

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


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
            for key in DEFAULT_CONFIG:
                config.setdefault(key, DEFAULT_CONFIG[key])
            return config
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load config: %s", e)
    return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    safe = {k: config.get(k, DEFAULT_CONFIG.get(k, "")) for k in DEFAULT_CONFIG}
    with open(CONFIG_FILE, "w") as f:
        json.dump(safe, f, indent=2)
    logger.info("Config saved to %s", CONFIG_FILE)
