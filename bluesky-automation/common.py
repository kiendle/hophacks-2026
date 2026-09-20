"""Shared paths and the existing semantic configuration compiler."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = ROOT / "bluesky-automation" / "data"
DEFAULT_URL = "http://127.0.0.1:8766"
DEFAULT_SOURCE = (
    "wss://jetstream.us-west.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents"
)

# The historical runner remains the source of truth for filtering and inference.
sys.path.insert(0, str(ROOT / "twitter-preparation"))
from configure_automation import _read_json, compile_configuration
from jev_protocol import configuration_key


def compile_config(config: dict[str, Any]) -> dict[str, Any]:
    compiled = compile_configuration(config)
    encoded = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        "config": config,
        "compiled": compiled,
        "config_hash": hashlib.sha256(encoded).hexdigest(),
        "configuration_key": configuration_key(
            compiled["company-categories.json"], compiled["jev-policy.json"]
        ),
    }


def read_config(path: str | Path) -> dict[str, Any]:
    config = _read_json(Path(path))
    compile_config(config)
    return config
