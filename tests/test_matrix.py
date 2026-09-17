"""Tests for the Matrix notifier. All HTTP calls are mocked — no network."""

import json
import unittest
import urllib.error
from unittest import mock

try:
    import _bootstrap  # noqa: F401
except ImportError:
    from tests import _bootstrap  # noqa: F401

from notifications import MatrixNotifier, NullNotifier, SafeNotifier, build_notifier
from notifications.matrix import format_duration


class RecordingTransport:
    def __init__(self) -> None:
        self.bodies = []

    def __call__(self, body: str) -> None:
        self.bodies.append(body)


def make_notifier(transport=None):
    return MatrixNotifier(
        homeserver="https://matrix.example.com",
        room_id="!room:example.com",
        access_token="secret-token",
        transport=transport,
    )


class BuildNotifierTests(unittest.TestCase):
    def test_disabled_returns_null_notifier(self):
        notifier = build_notifier(env={"MATRIX_ENABLED": "false"})
        self.assertIsInstance(notifier, NullNotifier)

    def test_unset_returns_null_notifier(self):
        self.assertIsInstance(build_notifier(env={}), NullNotifier)

    def test_enabled_missing_settings_raise(self):
        with self.assertRaises(ValueError) as ctx:
            build_notifier(env={"MATRIX_ENABLED": "true"})
        self.assertIn("MATRIX_HOMESERVER", str(ctx.exception))

    def test_enabled_partial_settings_raise(self):
        with self.assertRaises(ValueError):
            build_notifier(env={
                "MATRIX_ENABLED": "true",
                "MATRIX_HOMESERVER": "https://matrix.example.com",
            })

    def test_enabled_returns_safe_matrix_notifier(self):
        notifier = build_notifier(env={
            "MATRIX_ENABLED": "true",
            "MATRIX_HOMESERVER": "https://matrix.example.com",
            "MATRIX_ROOM_ID": "!room:example.com",
            "MATRIX_ACCESS_TOKEN": "token",
        })
        self.assertIsInstance(notifier, SafeNotifier)
        self.assertIsInstance(notifier.inner, MatrixNotifier)


class MatrixMessageTests(unittest.TestCase):
    def setUp(self):
        self.transport = RecordingTransport()
        self.notifier = make_notifier(self.transport)

    def sent(self):
        self.assertEqual(len(self.transport.bodies), 1)
        return self.transport.bodies[0]

    def test_task_started_message(self):
        self.notifier.task_started({
            "id": "f2-d8",
            "title": "Implement refresh token rotation",
            "started_at": "2026-09-17 03:02",
        })
        body = self.sent()
        self.assertIn("FlowForge task started", body)
        self.assertIn("f2-d8 — Implement refresh token rotation", body)
        self.assertIn("Started: 2026-09-17 03:02", body)

    def test_task_started_without_title(self):
        self.notifier.task_started({"id": "f2-d8"})
        self.assertIn("Task: f2-d8", self.sent())

    def test_task_completed_message(self):
        self.notifier.task_completed(
            {"id": "f2-d8", "title": "Implement refresh token rotation"},
            {"status": "done", "duration_sec": 1122.4, "log_path": "/tmp/runs/x.log"},
        )
        body = self.sent()
        self.assertIn("FlowForge task completed", body)
        self.assertIn("Duration: 18m 42s", body)
        self.assertIn("Status: done", body)

    def test_task_failed_message(self):
        self.notifier.task_failed(
            {"id": "f2-d8", "title": "Implement refresh token rotation"},
            {
                "stage": "Execution",
                "message": "OpenCode exited with code 1",
                "duration_sec": 668,
                "log_path": "/home/user/flow-forge/runs/f2-d8.log",
            },
        )
        body = self.sent()
        self.assertIn("FlowForge task failed", body)
        self.assertIn("Stage: Execution", body)
        self.assertIn("Duration: 11m 08s", body)
        self.assertIn("Error:\nOpenCode exited with code 1", body)
        self.assertIn("Log:\n/home/user/flow-forge/runs/f2-d8.log", body)

    def test_task_failed_error_truncated(self):
        self.notifier.task_failed(
            {"id": "x"},
            {"message": "E" * 5000, "duration_sec": 1},
        )
        body = self.sent()
        self.assertLess(len(body), 1000)

    def test_run_completed_message(self):
        self.notifier.run_completed({
            "results": [
                {"id": "f2-d8", "title": "Implement refresh token rotation", "ok": True},
                {"id": "f2-d9", "title": "Add validation", "ok": True},
                {"id": "f2-d10", "title": "Add integration tests", "ok": False},
            ],
            "completed": 2,
            "failed": 1,
            "duration_sec": 8220,
        })
        body = self.sent()
        self.assertIn("FlowForge run finished", body)
        self.assertIn("✅ f2-d8 — Implement refresh token rotation", body)
        self.assertIn("✅ f2-d9 — Add validation", body)
        self.assertIn("❌ f2-d10 — Add integration tests", body)
        self.assertIn("Completed: 2", body)
        self.assertIn("Failed: 1", body)
        self.assertIn("Duration: 2h 17m", body)


class MatrixHttpTests(unittest.TestCase):
    def test_send_builds_correct_request(self):
        notifier = make_notifier()
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            notifier.task_started({"id": "f2-d8"})
        urlopen.assert_called_once()
        (request,), kwargs = urlopen.call_args
        self.assertEqual(request.get_method(), "PUT")
        self.assertTrue(request.full_url.startswith(
            "https://matrix.example.com/_matrix/client/v3/rooms/%21room%3Aexample.com/send/m.room.message/m"
        ))
        self.assertEqual(request.headers.get("Authorization"), "Bearer secret-token")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["msgtype"], "m.text")
        self.assertIn("f2-d8", payload["body"])

    def test_http_error_does_not_raise_through_safe_notifier(self):
        notifier = SafeNotifier(make_notifier())
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = urllib.error.HTTPError(
                "https://matrix.example.com", 403, "Forbidden", {}, None
            )
            with self.assertLogs("flowforge.notifications", level="ERROR"):
                notifier.task_started({"id": "f2-d8"})
        self.assertTrue(urlopen.called)

    def test_url_error_does_not_raise_through_safe_notifier(self):
        notifier = SafeNotifier(make_notifier())
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = urllib.error.URLError("connection refused")
            with self.assertLogs("flowforge.notifications", level="ERROR"):
                notifier.run_completed({"results": [{"id": "x", "ok": True}], "completed": 1, "failed": 0})
        self.assertTrue(urlopen.called)

    def test_timeout_error_does_not_raise_through_safe_notifier(self):
        import socket

        notifier = SafeNotifier(make_notifier())
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = socket.timeout("timed out")
            with self.assertLogs("flowforge.notifications", level="ERROR"):
                notifier.task_failed({"id": "x"}, {"message": "boom"})
        self.assertTrue(urlopen.called)


class FormatDurationTests(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(1122), "18m 42s")
        self.assertEqual(format_duration(668), "11m 08s")
        self.assertEqual(format_duration(3600), "1h 00m")
        self.assertEqual(format_duration(8220), "2h 17m")


if __name__ == "__main__":
    unittest.main()
