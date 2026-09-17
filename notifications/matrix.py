"""Matrix notifier using the plain client-server HTTP API (no SDK)."""

from __future__ import annotations

import itertools
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from .base import Notifier

DEFAULT_TIMEOUT_SEC = 10.0
MAX_MESSAGE_CHARS = 2000
MAX_ERROR_CHARS = 500
USER_AGENT = "FlowForge/1.0"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def task_line(task: Dict[str, Any]) -> str:
    task_id = str(task.get("id") or "-")
    title = str(task.get("title") or "").strip()
    return f"{task_id} — {title}" if title else task_id


class MatrixNotifier(Notifier):
    """Sends short lifecycle messages to one Matrix room via PUT /send."""

    def __init__(
        self,
        homeserver: str,
        room_id: str,
        access_token: str,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        transport: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.room_id = room_id
        self.access_token = access_token
        self.timeout_sec = timeout_sec
        self.transport = transport
        self._txn_counter = itertools.count(1)

    def _send(self, body: str) -> None:
        body = body[:MAX_MESSAGE_CHARS]
        if self.transport is not None:
            self.transport(body)
            return
        txn_id = f"m{int(time.time() * 1000)}-{next(self._txn_counter)}"
        url = (
            self.homeserver
            + "/_matrix/client/v3/rooms/"
            + urllib.parse.quote(self.room_id, safe="")
            + f"/send/m.room.message/{txn_id}"
        )
        payload = json.dumps(
            {"msgtype": "m.text", "body": body}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="PUT",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Matrix homeserver returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Matrix request failed: {exc.reason}") from exc

    def task_started(self, task: Dict[str, Any]) -> None:
        lines = ["🔵 FlowForge task started", f"Task: {task_line(task)}"]
        started_at = str(task.get("started_at") or "").strip()
        if started_at:
            lines.append(f"Started: {started_at}")
        self._send("\n".join(lines))

    def task_completed(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        lines = ["🟢 FlowForge task completed", f"Task: {task_line(task)}"]
        if result.get("duration_sec") is not None:
            lines.append(f"Duration: {format_duration(float(result['duration_sec']))}")
        status = str(result.get("status") or "").strip()
        if status:
            lines.append(f"Status: {status}")
        self._send("\n".join(lines))

    def task_failed(self, task: Dict[str, Any], error: Dict[str, Any]) -> None:
        lines = ["🔴 FlowForge task failed", f"Task: {task_line(task)}"]
        stage = str(error.get("stage") or "").strip()
        if stage:
            lines.append(f"Stage: {stage}")
        if error.get("duration_sec") is not None:
            lines.append(f"Duration: {format_duration(float(error['duration_sec']))}")
        message = str(error.get("message") or "unknown error").strip()
        lines.append("Error:")
        lines.append(message[:MAX_ERROR_CHARS])
        log_path = str(error.get("log_path") or "").strip()
        if log_path:
            lines.append("Log:")
            lines.append(log_path)
        self._send("\n".join(lines))

    def run_completed(self, summary: Dict[str, Any]) -> None:
        lines = ["🌙 FlowForge run finished"]
        for item in summary.get("results", []):
            mark = "✅" if item.get("ok") else "❌"
            lines.append(f"{mark} {task_line(item)}")
        lines.append(f"Completed: {summary.get('completed', 0)}")
        lines.append(f"Failed: {summary.get('failed', 0)}")
        if summary.get("duration_sec") is not None:
            lines.append(f"Duration: {format_duration(float(summary['duration_sec']))}")
        self._send("\n".join(lines))
