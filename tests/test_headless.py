"""Tests for the headless batch runner (no OpenCode, no network)."""

import json
import tempfile
import unittest
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ImportError:
    from tests import _bootstrap  # noqa: F401

import headless_runner as hr
import pipeline_runner as pr
from notifications import NullNotifier
from notifications.base import SafeNotifier
from pipeline_runner import RunResult


class RecordingNotifier:
    def __init__(self) -> None:
        self.events = []

    def task_started(self, task):
        self.events.append(("task_started", task))

    def task_completed(self, task, result):
        self.events.append(("task_completed", task, result))

    def task_failed(self, task, error):
        self.events.append(("task_failed", task, error))

    def run_completed(self, summary):
        self.events.append(("run_completed", summary))


class BreakingNotifier(RecordingNotifier):
    def task_started(self, task):
        raise RuntimeError("matrix unreachable")

    def task_completed(self, task, result):
        raise RuntimeError("matrix unreachable")

    def task_failed(self, task, error):
        raise RuntimeError("matrix unreachable")

    def run_completed(self, summary):
        raise RuntimeError("matrix unreachable")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def write_tasks(tasks):
    pr.TASKS_FILE.write_text(
        "\n".join(json.dumps(t) for t in tasks) + "\n", encoding="utf-8"
    )


def make_task(task_id, status="todo"):
    return {"id": task_id, "status": status, "task_file": f"/tmp/{task_id}.md"}


def ok_result(task_id):
    return RunResult(True, task_id, "ses-1", Path("/tmp/runs/x.log"), f"{task_id} -> done")


def fail_result(task_id):
    return RunResult(False, task_id, None, Path("/tmp/runs/x.log"), f"{task_id} -> failed")


def stateful_run_task(result_factory):
    """Fake run_selected_task that also persists status like the real one."""

    def run_task(task_id, callback):
        rows = [json.loads(line) for line in pr.TASKS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
        result = result_factory(task_id)
        for row in rows:
            if row.get("id") == task_id:
                row["status"] = "done" if result.ok else "failed"
        pr.TASKS_FILE.write_text(
            "\n".join(json.dumps(t) for t in rows) + "\n", encoding="utf-8"
        )
        return result

    return run_task


class HeadlessTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_tasks_file = pr.TASKS_FILE
        self._orig_project_dir = pr.PROJECT_DIR
        tmp = Path(tempfile.mkdtemp(prefix="flowforge-case-"))
        pr.PROJECT_DIR = tmp / "project"
        pr.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        pr.TASKS_FILE = tmp / "tasks.jsonl"
        self.project_dir = pr.PROJECT_DIR
        self.notifier = RecordingNotifier()

    def tearDown(self):
        pr.TASKS_FILE = self._orig_tasks_file
        pr.PROJECT_DIR = self._orig_project_dir


class BatchTests(HeadlessTestCase):
    def test_batch_limit(self):
        write_tasks([make_task(f"t{i}") for i in range(5)])
        config = hr.BatchConfig(max_tasks=2, stop_on_failure=True, max_runtime_seconds=0)
        outcome = hr.run_batch(config, self.notifier, run_task=lambda task_id, callback: ok_result(task_id))
        self.assertEqual(len(outcome.outcomes), 2)
        self.assertEqual(outcome.reason, "max_tasks")
        self.assertEqual(outcome.exit_code, hr.EXIT_OK)
        self.assertEqual(len(self.notifier.events), 2 + 2 + 1)  # started+completed per task, 1 summary
        self.assertEqual(self.notifier.events[-1][1]["completed"], 2)

    def test_stop_on_failure_true(self):
        write_tasks([make_task("a"), make_task("b"), make_task("c")])
        config = hr.BatchConfig(max_tasks=5, stop_on_failure=True, max_runtime_seconds=0)
        outcome = hr.run_batch(config, self.notifier, run_task=lambda task_id, callback: fail_result(task_id))
        self.assertEqual(len(outcome.outcomes), 1)
        self.assertEqual(outcome.reason, "stop_on_failure")
        self.assertEqual(outcome.exit_code, hr.EXIT_TASK_FAILED)
        failed = [e for e in self.notifier.events if e[0] == "task_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("failed", failed[0][2]["message"])

    def test_stop_on_failure_false(self):
        write_tasks([make_task("a"), make_task("b")])
        config = hr.BatchConfig(max_tasks=5, stop_on_failure=False, max_runtime_seconds=0)
        outcome = hr.run_batch(
            config, self.notifier, run_task=stateful_run_task(fail_result)
        )
        self.assertEqual(len(outcome.outcomes), 2)
        self.assertEqual(outcome.failed, 2)
        self.assertEqual(outcome.exit_code, hr.EXIT_TASK_FAILED)
        self.assertEqual(outcome.reason, "queue_empty")

    def test_no_queued_tasks_is_silent(self):
        write_tasks([make_task("a", status="done"), make_task("b", status="failed")])
        config = hr.BatchConfig(max_tasks=3, stop_on_failure=True, max_runtime_seconds=0)
        outcome = hr.run_batch(config, self.notifier, run_task=lambda task_id, callback: ok_result(task_id))
        self.assertEqual(outcome.outcomes, [])
        self.assertEqual(outcome.reason, "queue_empty")
        self.assertEqual(outcome.exit_code, hr.EXIT_OK)
        self.assertEqual(self.notifier.events, [])

    def test_runtime_limit_prevents_next_task(self):
        write_tasks([make_task("a"), make_task("b"), make_task("c")])
        clock = FakeClock()

        def run_task(task_id, callback):
            result = ok_result(task_id)
            clock.advance(60)
            return result

        config = hr.BatchConfig(max_tasks=5, stop_on_failure=True, max_runtime_seconds=100)
        outcome = hr.run_batch(config, self.notifier, run_task=run_task, clock=clock)
        self.assertEqual(len(outcome.outcomes), 2)
        self.assertEqual(outcome.reason, "max_runtime")

    def test_runtime_limit_zero_is_unlimited(self):
        write_tasks([make_task("a"), make_task("b"), make_task("c")])
        clock = FakeClock()

        def run_task(task_id, callback):
            result = ok_result(task_id)
            clock.advance(5000)
            return result

        config = hr.BatchConfig(max_tasks=3, stop_on_failure=True, max_runtime_seconds=0)
        outcome = hr.run_batch(config, self.notifier, run_task=run_task, clock=clock)
        self.assertEqual(len(outcome.outcomes), 3)
        self.assertEqual(outcome.reason, "max_tasks")

    def test_single_task_id_runs_once(self):
        write_tasks([make_task("a"), make_task("b")])
        seen = []

        def run_task(task_id, callback):
            seen.append(task_id)
            return ok_result(task_id)

        config = hr.BatchConfig(max_tasks=1, stop_on_failure=True, max_runtime_seconds=0)
        outcome = hr.run_batch(config, self.notifier, task_id="b", run_task=run_task)
        self.assertEqual(seen, ["b"])
        self.assertEqual(outcome.reason, "completed")
        self.assertEqual(outcome.exit_code, hr.EXIT_OK)

    def test_unknown_task_id_raises(self):
        write_tasks([make_task("a")])
        config = hr.BatchConfig()
        with self.assertRaises(ValueError):
            hr.run_batch(config, self.notifier, task_id="nope", run_task=lambda task_id, callback: ok_result(task_id))

    def test_run_exception_counts_as_failure(self):
        write_tasks([make_task("a"), make_task("b")])

        def run_task(task_id, callback):
            raise RuntimeError("task file missing")

        config = hr.BatchConfig(max_tasks=3, stop_on_failure=True, max_runtime_seconds=0)
        outcome = hr.run_batch(config, self.notifier, run_task=run_task)
        self.assertEqual(outcome.failed, 1)
        self.assertEqual(outcome.exit_code, hr.EXIT_TASK_FAILED)
        failed = [e for e in self.notifier.events if e[0] == "task_failed"][0]
        self.assertIn("RuntimeError: task file missing", failed[2]["message"])

    def test_notification_failure_never_fails_the_batch(self):
        write_tasks([make_task("a")])
        config = hr.BatchConfig(max_tasks=1, stop_on_failure=True, max_runtime_seconds=0)
        with self.assertLogs("flowforge.notifications", level="ERROR"):
            outcome = hr.run_batch(
                config, SafeNotifier(BreakingNotifier()),
                run_task=lambda task_id, callback: ok_result(task_id),
            )
        self.assertEqual(outcome.completed, 1)
        self.assertEqual(outcome.exit_code, hr.EXIT_OK)

    def test_null_notifier_works(self):
        write_tasks([make_task("a")])
        config = hr.BatchConfig(max_tasks=1, stop_on_failure=True, max_runtime_seconds=0)
        outcome = hr.run_batch(config, NullNotifier(), run_task=lambda task_id, callback: ok_result(task_id))
        self.assertEqual(outcome.completed, 1)

    def test_stage_and_error_tracked_from_events(self):
        write_tasks([make_task("a")])

        def run_task(task_id, callback):
            callback({"kind": "run_start", "log_path": "/tmp/runs/a.log", "run_mode": "full", "steps_total": 3})
            callback({"kind": "step_start", "index": 2, "label": "Execution", "total": 3})
            callback({"kind": "error", "text": "Step 'Execution' failed: OpenCode exited with code 1"})
            return fail_result(task_id)

        config = hr.BatchConfig(max_tasks=1, stop_on_failure=True, max_runtime_seconds=0)
        hr.run_batch(config, self.notifier, run_task=run_task)
        failed = [e for e in self.notifier.events if e[0] == "task_failed"][0]
        self.assertEqual(failed[2]["stage"], "Execution")
        self.assertIn("OpenCode exited with code 1", failed[2]["message"])
        self.assertEqual(failed[2]["log_path"], "/tmp/runs/x.log")


class TaskTitleTests(HeadlessTestCase):
    def test_title_from_markdown_heading(self):
        task_file = self.project_dir / "task.md"
        task_file.write_text("# Implement refresh token rotation\n\nBody text\n", encoding="utf-8")
        self.assertEqual(hr.task_title({"task_file": task_file.name}, self.project_dir), "Implement refresh token rotation")

    def test_title_missing_file(self):
        self.assertIsNone(hr.task_title({"task_file": "nope.md"}, self.project_dir))

    def test_title_no_heading(self):
        task_file = self.project_dir / "task2.md"
        task_file.write_text("Just body text without heading\n", encoding="utf-8")
        self.assertIsNone(hr.task_title({"task_file": str(task_file)}, self.project_dir))

    def test_title_truncated(self):
        task_file = self.project_dir / "task3.md"
        task_file.write_text("# " + "x" * 500 + "\n", encoding="utf-8")
        self.assertEqual(len(hr.task_title({"task_file": str(task_file)}, self.project_dir)), hr.TITLE_MAX_CHARS)

    def test_title_no_task_file(self):
        self.assertIsNone(hr.task_title({}, self.project_dir))


class BatchConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = hr.batch_config_from_env(env={})
        self.assertEqual(config.max_tasks, 1)
        self.assertTrue(config.stop_on_failure)
        self.assertEqual(config.max_runtime_seconds, 14400)

    def test_env_overrides(self):
        config = hr.batch_config_from_env(env={
            "FLOWFORGE_MAX_TASKS_PER_RUN": "3",
            "FLOWFORGE_STOP_ON_FAILURE": "false",
            "FLOWFORGE_MAX_RUNTIME_SECONDS": "7200",
        })
        self.assertEqual(config.max_tasks, 3)
        self.assertFalse(config.stop_on_failure)
        self.assertEqual(config.max_runtime_seconds, 7200)

    def test_invalid_int_raises(self):
        with self.assertRaises(ValueError):
            hr.batch_config_from_env(env={"FLOWFORGE_MAX_TASKS_PER_RUN": "abc"})

    def test_invalid_max_tasks_too_small(self):
        with self.assertRaises(ValueError):
            hr.batch_config_from_env(env={"FLOWFORGE_MAX_TASKS_PER_RUN": "0"})

    def test_max_tasks_override(self):
        config = hr.batch_config_from_env(env={}, max_tasks_override=7)
        self.assertEqual(config.max_tasks, 7)

    def test_max_tasks_override_invalid(self):
        with self.assertRaises(ValueError):
            hr.batch_config_from_env(env={}, max_tasks_override=0)


class LockTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="flowforge-lock-"))
        self.lock_path = self.tmp / "runs" / ".flowforge.lock"

    def test_lock_acquire_release(self):
        handle = hr.acquire_lock(self.lock_path)
        self.assertIsNotNone(handle)
        hr.release_lock(handle)
        handle2 = hr.acquire_lock(self.lock_path)
        self.assertIsNotNone(handle2)
        hr.release_lock(handle2)

    def test_lock_blocks_second_acquire(self):
        handle = hr.acquire_lock(self.lock_path)
        self.assertIsNone(hr.acquire_lock(self.lock_path))
        hr.release_lock(handle)


if __name__ == "__main__":
    unittest.main()
