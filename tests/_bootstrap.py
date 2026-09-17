"""Isolate env before pipeline_runner is imported by any test module."""

import os
import tempfile
from pathlib import Path

_BASE = Path(tempfile.mkdtemp(prefix="flowforge-tests-"))
_PROJECT = _BASE / "project"
_PROJECT.mkdir(parents=True, exist_ok=True)

os.environ["PROJECT_DIR"] = str(_PROJECT)
os.environ["TASKS_FILE"] = str(_PROJECT / "tasks.jsonl")
os.environ["MATRIX_ENABLED"] = "false"
