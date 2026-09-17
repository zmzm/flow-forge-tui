"""Generic notifier interface for FlowForge lifecycle events."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

logger = logging.getLogger("flowforge.notifications")

TRUE_VALUES = {"1", "true", "yes", "on"}


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in TRUE_VALUES


class Notifier(ABC):
    """Lifecycle notification contract.

    Payload dicts (all keys except ids are optional):
      task_started(task): task = {id, title, status, started_at}
      task_completed(task, result): result = {status, duration_sec, log_path}
      task_failed(task, error): error = {stage, message, duration_sec, log_path}
      run_completed(summary): summary = {results: [{id, title, ok}], completed, failed, duration_sec}
    """

    @abstractmethod
    def task_started(self, task: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def task_completed(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def task_failed(self, task: Dict[str, Any], error: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def run_completed(self, summary: Dict[str, Any]) -> None:
        raise NotImplementedError


class NullNotifier(Notifier):
    """No-op notifier used when notifications are disabled."""

    def task_started(self, task: Dict[str, Any]) -> None:
        pass

    def task_completed(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        pass

    def task_failed(self, task: Dict[str, Any], error: Dict[str, Any]) -> None:
        pass

    def run_completed(self, summary: Dict[str, Any]) -> None:
        pass


class SafeNotifier(Notifier):
    """Wraps a notifier so failures are logged and never reach the pipeline."""

    def __init__(self, inner: Notifier) -> None:
        self.inner = inner

    def task_started(self, task: Dict[str, Any]) -> None:
        self._call("task_started", task)

    def task_completed(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        self._call("task_completed", task, result)

    def task_failed(self, task: Dict[str, Any], error: Dict[str, Any]) -> None:
        self._call("task_failed", task, error)

    def run_completed(self, summary: Dict[str, Any]) -> None:
        self._call("run_completed", summary)

    def _call(self, method: str, *args: Any) -> None:
        try:
            getattr(self.inner, method)(*args)
        except Exception:
            logger.exception("Failed to send notification: %s", method)
