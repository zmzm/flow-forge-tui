"""Tests for pipeline_runner internals: parsing, step selection, persistence, timeouts."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _bootstrap  # noqa: F401
except ImportError:
    from tests import _bootstrap  # noqa: F401

import pipeline_runner as pr

FAKE_OK = r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
files = [Path(args[i + 1]) for i, a in enumerate(args) if a == "--file"]
src = files[0]
for name in ("concept-plan.md", "grounded-plan.md", "execution.md"):
    (src.parent / name).write_text("# " + name + "\n", encoding="utf-8")
print(json.dumps({"type": "event", "sessionID": "ses-fake"}))
"""

FAKE_FAIL = r"""#!/usr/bin/env python3
import json
import sys

print(json.dumps({"type": "error", "error": {"name": "ProviderError", "data": {"message": "rate limited"}}}))
sys.exit(1)
"""

FAKE_HANG = r"""#!/usr/bin/env python3
import json
import time

print(json.dumps({"type": "event", "sessionID": "ses-hang"}), flush=True)
time.sleep(60)
"""


class ParseTests(unittest.TestCase):
    def test_parse_session_id_variants(self):
        self.assertEqual(pr.parse_session_id_from_json_events('{"sessionID": "s1"}'), "s1")
        self.assertEqual(pr.parse_session_id_from_json_events('{"part": {"sessionId": "s2"}}'), "s2")
        self.assertEqual(pr.parse_session_id_from_json_events('{"metadata": {"id": "s3"}}'), "s3")
        self.assertIsNone(pr.parse_session_id_from_json_events('{"type": "text"}\nnot json'))

    def test_parse_opencode_error(self):
        out = '{"type":"error","error":{"name":"ProviderError","data":{"message":"rate limited","ref":"r1"}}}'
        self.assertEqual(
            pr.parse_opencode_error_from_json_events(out),
            "ProviderError: rate limited (ref: r1)",
        )
        self.assertIsNone(pr.parse_opencode_error_from_json_events('{"type":"text"}'))
        self.assertEqual(
            pr.parse_opencode_error_from_json_events('{"type":"error","error":"boom"}'),
            "boom",
        )


class OptionalIntEnvTests(unittest.TestCase):
    def test_default_when_unset(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FLOWFORGE_TEST_INT", None)
            self.assertEqual(pr._optional_int_env("FLOWFORGE_TEST_INT", 5), 5)

    def test_zero_and_negative_disable(self):
        with mock.patch.dict(os.environ, {"FLOWFORGE_TEST_INT": "0"}):
            self.assertIsNone(pr._optional_int_env("FLOWFORGE_TEST_INT", 5))
        with mock.patch.dict(os.environ, {"FLOWFORGE_TEST_INT": "-3"}):
            self.assertIsNone(pr._optional_int_env("FLOWFORGE_TEST_INT", 5))

    def test_value(self):
        with mock.patch.dict(os.environ, {"FLOWFORGE_TEST_INT": "120"}):
            self.assertEqual(pr._optional_int_env("FLOWFORGE_TEST_INT", 5), 120)

    def test_invalid_raises(self):
        with mock.patch.dict(os.environ, {"FLOWFORGE_TEST_INT": "abc"}):
            with self.assertRaises(RuntimeError):
                pr._optional_int_env("FLOWFORGE_TEST_INT", 5)


class StepSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flowforge-steps-"))
        self.out_dir = self.tmp / "outputs"
        self.out_dir.mkdir()

    def steps_fixture(self):
        return [
            {
                "id": step_id,
                "label": step_id.title(),
                "agent": "agent-" + step_id,
                "output_file": self.out_dir / f"{step_id}.md",
            }
            for step_id in ("concept", "grounded", "execution")
        ]

    def touch_outputs(self, *step_ids):
        for step_id in step_ids:
            (self.out_dir / f"{step_id}.md").write_text("x", encoding="utf-8")

    def test_full_run_for_todo(self):
        steps, mode = pr.select_steps_for_task({"status": "todo"}, self.steps_fixture())
        self.assertEqual([s["id"] for s in steps], ["concept", "grounded", "execution"])
        self.assertEqual(mode, "full")

    def test_explicit_run_steps(self):
        steps, mode = pr.select_steps_for_task({"run_steps": ["execution"]}, self.steps_fixture())
        self.assertEqual([s["id"] for s in steps], ["execution"])
        self.assertEqual(mode, "explicit")

    def test_unknown_run_steps_raise(self):
        with self.assertRaises(ValueError):
            pr.select_steps_for_task({"run_steps": ["nope"]}, self.steps_fixture())

    def test_failed_resumes_from_missing_output(self):
        self.touch_outputs("concept")
        steps, mode = pr.select_steps_for_task({"status": "failed"}, self.steps_fixture())
        self.assertEqual([s["id"] for s in steps], ["grounded", "execution"])
        self.assertEqual(mode, "resume")

    def test_failed_all_outputs_already_complete(self):
        self.touch_outputs("concept", "grounded", "execution")
        steps, mode = pr.select_steps_for_task({"status": "failed"}, self.steps_fixture())
        self.assertEqual(steps, [])
        self.assertEqual(mode, "already_complete")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flowforge-persist-"))
        self.tasks_file = self.tmp / "tasks.jsonl"

    def write_rows(self, rows):
        self.tasks_file.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

    def rows(self):
        return pr.read_tasks_jsonl(self.tasks_file)

    def test_atomic_write_leaves_no_temp_files(self):
        pr.write_tasks_jsonl(self.tasks_file, [{"id": "a", "status": "todo"}])
        pr.write_tasks_jsonl(self.tasks_file, [{"id": "a", "status": "done"}])
        self.assertEqual(self.rows()[0]["status"], "done")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_persist_update_merges_by_id_after_reorder(self):
        self.write_rows([{"id": "a", "status": "todo"}, {"id": "b", "status": "todo"}])
        self.write_rows([{"id": "b", "status": "todo"}, {"id": "a", "status": "todo"}])
        pr._persist_task_update(self.tasks_file, 0, "a", {"status": "done"})
        rows = self.rows()
        self.assertEqual([r["id"] for r in rows], ["b", "a"])
        self.assertEqual(rows[1]["status"], "done")
        self.assertEqual(rows[0]["status"], "todo")

    def test_persist_update_appends_missing_task(self):
        self.write_rows([{"id": "a", "status": "todo"}])
        pr._persist_task_update(self.tasks_file, 9, "zz", {"status": "done"})
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], {"id": "zz", "status": "done"})

    def test_recover_stale_running_tasks(self):
        self.write_rows([
            {"id": "x", "status": "running", "task_file": "/tmp/x.md"},
            {"id": "y", "status": "todo", "task_file": "/tmp/y.md"},
            {"id": "z", "status": "done", "task_file": "/tmp/z.md"},
        ])
        recovered = pr.recover_stale_running_tasks(self.tasks_file)
        self.assertEqual(recovered, ["x"])
        rows = {r["id"]: r["status"] for r in self.rows()}
        self.assertEqual(rows, {"x": "failed", "y": "todo", "z": "done"})

    def test_recover_nothing_to_do(self):
        self.write_rows([{"id": "y", "status": "todo"}])
        self.assertEqual(pr.recover_stale_running_tasks(self.tasks_file), [])

    def test_recover_missing_file(self):
        self.assertEqual(pr.recover_stale_running_tasks(self.tmp / "nope.jsonl"), [])


class PipelineIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flowforge-int-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        self._orig = {
            name: getattr(pr, name)
            for name in ("TASKS_FILE", "STEPS_FILE", "PROJECT_DIR", "RUNS_DIR")
        }
        pr.TASKS_FILE = self.project / "tasks.jsonl"
        pr.STEPS_FILE = self.project / "steps.json"
        pr.PROJECT_DIR = self.project
        pr.RUNS_DIR = self.tmp / "runs"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(pr, name, value)

    def make_opencode(self, name, body):
        script = self.tmp / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def add_task(self, status="todo"):
        (self.project / "task.md").write_text("# Sample task\n", encoding="utf-8")
        pr.write_tasks_jsonl(
            pr.TASKS_FILE, [{"id": "t1", "task_file": "task.md", "status": status}]
        )

    def task_row(self):
        return pr.read_tasks_jsonl(pr.TASKS_FILE)[0]


class RunSelectedTaskTests(PipelineIntegrationTestCase):
    def test_success_marks_done_and_persists_outputs(self):
        self.add_task()
        bin_path = self.make_opencode("fake-ok", FAKE_OK)
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": str(bin_path)}):
            result = pr.run_selected_task(task_id="t1")
        self.assertTrue(result.ok)
        row = self.task_row()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["session_id"], "ses-fake")
        self.assertEqual(set(row["outputs"]), {"concept", "grounded", "execution"})
        self.assertIn("last_run_log", row)
        self.assertIsNone(result.stage)
        self.assertEqual(list(self.project.glob("*.tmp")), [])

    def test_provider_error_marks_failed_with_stage(self):
        self.add_task()
        bin_path = self.make_opencode("fake-fail", FAKE_FAIL)
        events = []
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": str(bin_path)}):
            result = pr.run_selected_task(task_id="t1", callback=events.append)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "Business plan")
        self.assertEqual(self.task_row()["status"], "failed")
        errors = [e["text"] for e in events if e.get("kind") == "error"]
        self.assertTrue(any("ProviderError: rate limited" in t for t in errors))

    def test_step_timeout_terminates_and_fails(self):
        self.add_task()
        bin_path = self.make_opencode("fake-hang", FAKE_HANG)
        events = []
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": str(bin_path)}), \
                mock.patch.object(pr, "STEP_TIMEOUT_SEC", 1):
            result = pr.run_selected_task(task_id="t1", callback=events.append)
        self.assertFalse(result.ok)
        self.assertEqual(self.task_row()["status"], "failed")
        errors = [e["text"] for e in events if e.get("kind") == "error"]
        self.assertTrue(any("timed out after 1s" in t for t in errors))

    def test_missing_binary_persists_failed_and_raises(self):
        self.add_task()
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": "/nonexistent/opencode-bin"}):
            with self.assertRaises(FileNotFoundError):
                pr.run_selected_task(task_id="t1")
        self.assertEqual(self.task_row()["status"], "failed")

    def test_already_complete_failed_task_marks_done(self):
        for name in ("concept-plan.md", "grounded-plan.md", "execution.md"):
            (self.project / name).write_text("x", encoding="utf-8")
        self.add_task(status="failed")
        result = pr.run_selected_task(task_id="t1")
        self.assertTrue(result.ok)
        self.assertEqual(self.task_row()["status"], "done")

    def test_failed_task_resumes_from_missing_output(self):
        (self.project / "concept-plan.md").write_text("x", encoding="utf-8")
        self.add_task(status="failed")
        bin_path = self.make_opencode("fake-ok2", FAKE_OK)
        events = []
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": str(bin_path)}):
            result = pr.run_selected_task(task_id="t1", callback=events.append)
        self.assertTrue(result.ok)
        starts = [e.get("label") for e in events if e.get("kind") == "step_start"]
        self.assertEqual(starts, ["Technical plan", "Execution"])
        self.assertEqual(self.task_row()["status"], "done")


if __name__ == "__main__":
    unittest.main()
