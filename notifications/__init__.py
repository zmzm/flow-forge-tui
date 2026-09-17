"""Notification providers for FlowForge lifecycle events."""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

from .base import Notifier, NullNotifier, SafeNotifier, parse_bool
from .matrix import MatrixNotifier

__all__ = [
    "Notifier",
    "NullNotifier",
    "SafeNotifier",
    "MatrixNotifier",
    "parse_bool",
    "build_notifier",
]


def build_notifier(env: Optional[Mapping[str, str]] = None) -> Notifier:
    env = os.environ if env is None else env
    if not parse_bool(env.get("MATRIX_ENABLED", "")):
        return NullNotifier()
    settings = {
        "MATRIX_HOMESERVER": str(env.get("MATRIX_HOMESERVER", "")).strip(),
        "MATRIX_ROOM_ID": str(env.get("MATRIX_ROOM_ID", "")).strip(),
        "MATRIX_ACCESS_TOKEN": str(env.get("MATRIX_ACCESS_TOKEN", "")).strip(),
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise ValueError(
            "MATRIX_ENABLED is true but missing: " + ", ".join(missing)
        )
    return SafeNotifier(
        MatrixNotifier(
            homeserver=settings["MATRIX_HOMESERVER"],
            room_id=settings["MATRIX_ROOM_ID"],
            access_token=settings["MATRIX_ACCESS_TOKEN"],
            timeout_sec=float(env.get("MATRIX_TIMEOUT_SECONDS", "") or 10.0),
        )
    )
