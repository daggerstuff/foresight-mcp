"""Configuration management for Foresight CLI.

Config lives at ~/.foresight/config.json with env var overrides.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".foresight"
CONFIG_PATH = CONFIG_DIR / "config.json"

# Env var → config key mapping
ENV_MAP: dict[str, str] = {
    "FORESIGHT_DB_PATH": "db_path",
    "FORESIGHT_USER_ID": "user_id",
    "FORESIGHT_BANK_ID": "bank_id",
}

DEFAULTS: dict[str, Any] = {
    "db_path": str(CONFIG_DIR / "memory.db"),
    "user_id": os.environ.get("USER", "default"),
    "bank_id": "default",
    "theme": "auto",
    "timeout": 30000,
    "tui": {
        "show_preview": True,
        "preview_lines": 10,
        "compact_mode": False,
    },
}


@dataclass
class TuiConfig:
    show_preview: bool = True
    preview_lines: int = 10
    compact_mode: bool = False


@dataclass
class CliConfig:
    db_path: str = DEFAULTS["db_path"]
    user_id: str = DEFAULTS["user_id"]
    bank_id: str = DEFAULTS["bank_id"]
    theme: str = DEFAULTS["theme"]
    timeout: int = DEFAULTS["timeout"]
    tui: TuiConfig = field(default_factory=TuiConfig)

    @classmethod
    def load(cls) -> CliConfig:
        """Load config from file with env var overrides."""
        config_data = dict(DEFAULTS)

        # Load from file if exists
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text())
                config_data.update(raw)
            except (json.JSONDecodeError, OSError):
                pass

        # Apply env var overrides
        for env_var, config_key in ENV_MAP.items():
            if value := os.environ.get(env_var):
                config_data[config_key] = value

        # Handle nested tui config
        tui_data = config_data.pop("tui", {})
        if isinstance(tui_data, dict):
            tui_config = TuiConfig(**{k: v for k, v in tui_data.items() if k in TuiConfig.__dataclass_fields__})
        else:
            tui_config = TuiConfig()

        return cls(
            **{k: v for k, v in config_data.items() if k in cls.__dataclass_fields__ and k != "tui"}, tui=tui_config
        )

    def save(self) -> None:
        """Save config to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "db_path": self.db_path,
            "user_id": self.user_id,
            "bank_id": self.bank_id,
            "theme": self.theme,
            "timeout": self.timeout,
            "tui": {
                "show_preview": self.tui.show_preview,
                "preview_lines": self.tui.preview_lines,
                "compact_mode": self.tui.compact_mode,
            },
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2))
        CONFIG_PATH.chmod(0o600)


def ensure_config() -> CliConfig:
    """Ensure config directory and default config exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)

    if not CONFIG_PATH.exists():
        CliConfig().save()

    return CliConfig.load()


def get_db_path() -> str:
    """Get the resolved database path."""
    cfg = ensure_config()
    return cfg.db_path


def get_user_id(override: str | None = None) -> str | None:
    """Get user ID from override, config, or env."""
    if override:
        return override
    cfg = ensure_config()
    return cfg.user_id
