#!/usr/bin/env python3
"""
Headless batch runner around pipeline_runner.
Adds batch limits, single-instance locking, lifecycle logging, and notifications.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, IO, List, Optional

import pipeline_runner as pr
from notifications import Notifier
from notifications.base import parse_bool

logger = logging.getLogger("flowforge.runner")

DEFAULT_MAX_TASKS_PER_RUN = 1
DEFAULT_STOP_ON_FAILURE = True
DEFAULT_MAX_RUNTIME_SECONDS = 14400

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_CONFIG = 2

TITLE_MAX_CHARS = 100
TITLE_READ_CHARS = 4096
LOG_TEXT_LIMIT = 300


@dataclass
class BatchConfig:
    max_tasks: int = DEFAULT_MAX_TASKS_PER_RUN
    stop_on_failure: bool = DEFAULT_STOP_ON_FAILURE
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS


@dataclass
class TaskOutcome:
    task_id: str
    title: Optional[str]
    ok: bool
    message: str
    log_path: Optional[str]
    duration_sec: float


@dataclass
class BatchOutcome:
    outcomes: List[TaskOutcome] = field(default_factory=list)
    reason: str = "queue_empty"
    duration_sec: float = 0.0

    @property
    def completed(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.ok)

    @property
    def exit_code(self) -> int:
        return EXIT_TASK_FAILED if self.failed else EXIT_OK


def _int_env(env: Dict[str, str], name: str, default: int, minimum: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got: {value}")
    return value


def batch_config_from_env(
    env: Optional[Dict[str, str]] = None,
    max_tasks_override: Optional[int] = None,
) -> BatchConfig:
    env = os.environ if env is None else dict(env)
    if max_tasks_override is not None:
        if max_tasks_override < 1:
            raise ValueError("--max-tasks must be >= 1")
        max_tasks = max_tasks_override
    else:
        max_tasks = _int_env(env, "FLOWFORGE_MAX_TASKS_PER_RUN", DEFAULT_MAX_TASKS_PER_RUN, 1)
    stop_raw = str(env.get("FLOWFORGE_STOP_ON_FAILURE", "")).strip()
    return BatchConfig(
        max_tasks=max_tasks,
        stop_on_failure=parse_bool(stop_raw) if stop_raw else DEFAULT_STOP_ON_FAILURE,
        max_runtime_seconds=_int_env(
            env, "FLOWFORGE_MAX_RUNTIME_SECONDS", DEFAULT_MAX_RUNTIME_SECONDS, 0
        ),
    )


def acquire_lock(path: Path) -> Optional[IO[str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        logger.warning("Lock %s is held by another FlowForge process", path)
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def release_lock(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def task_title(task: Dict[str, Any], project_dir: Path) -> Optional[str]:
    """Best-effort task title: first Markdown heading of the task file."""
    raw = task.get("task_file")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            chunk = fh.read(TITLE_READ_CHARS)
    except OSError:
        return None
    for line in chunk.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            title = text.lstrip("#").strip()
            if title:
                return title[:TITLE_MAX_CHARS]
        break
    return None


class _RunTracker:
    """Event callback that records the current stage/error and logs lifecycle events."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.stage: Optional[str] = None
        self.error: Optional[str] = None
        self.log_path: Optional[str] = None

    def __call__(self, event: Dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "run_start":
            self.log_path = str(event.get("log_path") or "") or None
            logger.info(
                "Task %s started (mode=%s, steps=%s, log=%s)",
                self.task_id,
                event.get("run_mode"),
                event.get("steps_total"),
                self.log_path or "-",
            )
        elif kind == "step_start":
            self.stage = str(event.get("label") or "") or None
            logger.info(
                "Stage started: %s (%s/%s)",
                self.stage,
                int(event.get("index", 0)) + 1,
                event.get("total", "?"),
            )
        elif kind == "step_done":
            logger.info("Stage completed: %s", event.get("label"))
        elif kind == "error":
            self.error = str(event.get("text") or "") or None
            logger.warning("Pipeline error: %s", (self.error or "")[:LOG_TEXT_LIMIT])
        elif kind == "run_done":
            logger.info("Task %s finished ok=%s", self.task_id, bool(event.get("ok")))


def run_batch(
    config: BatchConfig,
    notifier: Notifier,
    task_id: Optional[str] = None,
    run_task: Optional[Callable[..., pr.RunResult]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> BatchOutcome:
    """Process queued tasks. With task_id set, runs exactly that one task."""
    run_one = run_task or pr.run_selected_task
    single = task_id is not None
    started = clock()
    outcomes: List[TaskOutcome] = []
    reason = "queue_empty"
    processed = 0

    while True:
        if single:
            if processed >= 1:
                reason = "completed"
                break
        else:
            if processed >= config.max_tasks:
                reason = "max_tasks"
                break
            if (
                processed > 0
                and config.max_runtime_seconds
                and (clock() - started) >= config.max_runtime_seconds
            ):
                reason = "max_runtime"
                logger.info(
                    "Max runtime (%ss) reached; not starting another task",
                    config.max_runtime_seconds,
                )
                break

        try:
            tasks = pr.read_tasks_jsonl(pr.TASKS_FILE)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"Cannot read tasks file {pr.TASKS_FILE}: {exc}") from exc

        idx = pr.find_task_index(tasks, task_id) if single else pr.find_next_todo(tasks)
        if idx is None:
            if single:
                raise ValueError(f"Task id not found: {task_id}")
            reason = "queue_empty"
            break

        task = tasks[idx]
        selected_id = str(task.get("id") or f"task{idx + 1}")
        title = task_title(task, pr.PROJECT_DIR)
        logger.info("Task selected: %s", selected_id)
        notifier.task_started(
            {
                "id": selected_id,
                "title": title,
                "status": task.get("status"),
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        )

        tracker = _RunTracker(selected_id)
        task_started_at = clock()
        result: Optional[pr.RunResult] = None
        failure: Optional[BaseException] = None
        try:
            result = run_one(task_id=selected_id, callback=tracker)
        except Exception as exc:
            failure = exc
            logger.exception("Task %s raised an exception", selected_id)
        duration = clock() - task_started_at

        if result is not None and result.ok:
            message = result.message
            log_path = str(result.log_path) if result.log_path else None
            print(message)
            if log_path:
                print(f"Log: {log_path}")
            if result.session_id:
                print(f"Session: {result.session_id}")
            notifier.task_completed(
                {"id": selected_id, "title": title},
                {"status": "done", "duration_sec": duration, "log_path": log_path},
            )
            outcomes.append(TaskOutcome(selected_id, title, True, message, log_path, duration))
        else:
            if result is not None:
                message = result.message
                log_path = str(result.log_path) if result.log_path else tracker.log_path
            else:
                message = f"{type(failure).__name__}: {failure}"
                log_path = tracker.log_path
            logger.error("Task %s failed: %s", selected_id, message[:LOG_TEXT_LIMIT])
            notifier.task_failed(
                {"id": selected_id, "title": title},
                {
                    "stage": tracker.stage,
                    "message": tracker.error or message,
                    "duration_sec": duration,
                    "log_path": log_path,
                },
            )
            outcomes.append(TaskOutcome(selected_id, title, False, message, log_path, duration))

        processed += 1
        if not outcomes[-1].ok and config.stop_on_failure:
            reason = "stop_on_failure"
            break

    duration = clock() - started
    if outcomes:
        notifier.run_completed(
            {
                "results": [
                    {"id": o.task_id, "title": o.title, "ok": o.ok} for o in outcomes
                ],
                "completed": sum(1 for o in outcomes if o.ok),
                "failed": sum(1 for o in outcomes if not o.ok),
                "duration_sec": duration,
            }
        )
    elif reason == "queue_empty":
        print("No TODO tasks.")
    logger.info(
        "Batch finished: reason=%s processed=%d completed=%d failed=%d duration=%.0fs",
        reason,
        len(outcomes),
        sum(1 for o in outcomes if o.ok),
        sum(1 for o in outcomes if not o.ok),
        duration,
    )
    return BatchOutcome(outcomes, reason, duration)
