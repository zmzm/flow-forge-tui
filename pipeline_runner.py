#!/usr/bin/env python3
"""
Headless pipeline runner.
Contains task selection, pipeline execution, and tasks.jsonl updates.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def _load_dotenv_if_present(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and ((value[0] == value[-1]) and value[0] in {"\"", "'"}):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv_if_present(Path(".env"))

def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


PROJECT_DIR = Path(_require_env("PROJECT_DIR")).expanduser()
TASKS_FILE = Path(_require_env("TASKS_FILE")).expanduser()
RUNS_DIR = Path("runs").expanduser()

AGENT_CONCEPT = "concept-plan"
AGENT_GROUNDED = "grounded-hard"
AGENT_EXECUTE = "execution-plan"
DEFAULT_PIPELINE_STEPS = ["concept", "grounded", "execution"]

MODEL: Optional[str] = _require_env("MODEL")
VARIANT: Optional[str] = None
USE_ATTACH = False
ATTACH_URL = "http://localhost:4096"
STEP_TIMEOUT_SEC: Optional[int] = None
SHARE_SESSION = False

EventCallback = Optional[Callable[[Dict[str, Any]], None]]


@dataclass
class RunResult:
    ok: bool
    task_id: Optional[str]
    session_id: Optional[str]
    log_path: Optional[Path]
    message: str


class RunControl:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stop_requested = False
        self._active_proc: Optional[subprocess.Popen[str]] = None

    def set_active_proc(self, proc: Optional[subprocess.Popen[str]]) -> None:
        with self._lock:
            self._active_proc = proc

    def request_stop(self) -> None:
        with self._lock:
            self.stop_requested = True
            proc = self._active_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def emit(callback: EventCallback, payload: Dict[str, Any]) -> None:
    if callback:
        callback(payload)


def read_tasks_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Tasks file not found: {path}")
    tasks: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            tasks.append(json.loads(line))
    return tasks


def write_tasks_jsonl(path: Path, tasks: List[Dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n",
        encoding="utf-8",
    )


def find_next_todo(tasks: List[Dict[str, Any]]) -> Optional[int]:
    for i, t in enumerate(tasks):
        if t.get("status", "todo") == "todo":
            return i
    return None


def find_task_index(tasks: List[Dict[str, Any]], task_id: str) -> Optional[int]:
    for i, t in enumerate(tasks):
        if (t.get("id") or f"task{i+1}") == task_id:
            return i
    return None


def resolve_file(path_like: str, base: Path) -> Path:
    p = Path(path_like).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def require_exists(p: Path, err_msg: str) -> None:
    if not p.exists():
        raise FileNotFoundError(f"{err_msg}: {p}")


def build_cmd_base(files: List[Path]) -> List[str]:
    cmd = ["opencode", "run", "--format", "json"]
    if USE_ATTACH:
        cmd += ["--attach", ATTACH_URL]
    if MODEL:
        cmd += ["--model", MODEL]
    if VARIANT:
        cmd += ["--variant", VARIANT]
    if SHARE_SESSION:
        cmd += ["--share"]
    for f in files:
        cmd += ["--file", str(f)]
    return cmd


def parse_session_id_from_json_events(output: str) -> Optional[str]:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue

        for key in ("sessionID", "sessionId", "session_id", "session"):
            val = ev.get(key)
            if isinstance(val, str) and val:
                return val

        part = ev.get("part")
        if isinstance(part, dict):
            sid = part.get("sessionID") or part.get("sessionId") or part.get("session_id")
            if isinstance(sid, str) and sid:
                return sid

        meta = ev.get("metadata")
        if isinstance(meta, dict):
            sid = meta.get("sessionID") or meta.get("sessionId") or meta.get("session_id") or meta.get("id")
            if isinstance(sid, str) and sid:
                return sid
    return None


def build_pipeline_steps(
    *,
    task: Dict[str, Any],
    task_file: Path,
    concept_plan_file: Path,
    grounded_plan_file: Path,
    execution_file: Path,
) -> List[Dict[str, Any]]:
    allowed = {"concept", "grounded", "execution"}
    requested_steps = task.get("steps") or DEFAULT_PIPELINE_STEPS
    if not isinstance(requested_steps, list) or not requested_steps:
        raise ValueError("Task field 'steps' must be a non-empty list if provided")
    unknown = [s for s in requested_steps if s not in allowed]
    if unknown:
        raise ValueError(f"Unknown step(s) in 'steps': {unknown}. Allowed: {sorted(allowed)}")

    agents_override = task.get("agents") or {}
    if not isinstance(agents_override, dict):
        raise ValueError("Task field 'agents' must be an object if provided")
    labels_override = task.get("step_labels") or {}
    if not isinstance(labels_override, dict):
        raise ValueError("Task field 'step_labels' must be an object if provided")

    step_defs: Dict[str, Dict[str, Any]] = {
        "concept": {
            "label": str(labels_override.get("concept") or "Concept plan"),
            "agent": str(agents_override.get("concept") or AGENT_CONCEPT),
            "input_file": task_file,
            "output_file": concept_plan_file,
            "message": (
                "Read the attached task file and produce a CONCEPT PLAN.\n"
                "Hard requirements:\n"
                "- Save your output ONLY to a file named 'concept-plan.md' in the SAME DIRECTORY as the task file.\n"
                "After writing the file, briefly confirm that 'concept-plan.md' was created."
            ),
        },
        "grounded": {
            "label": str(labels_override.get("grounded") or "Grounded plan"),
            "agent": str(agents_override.get("grounded") or AGENT_GROUNDED),
            "input_file": concept_plan_file,
            "output_file": grounded_plan_file,
            "message": (
                "Read ONLY the attached concept plan and produce a GROUNDED PLAN.\n"
                "Hard requirements:\n"
                "- Save your output ONLY to 'grounded-plan.md' in the SAME DIRECTORY as the concept plan.\n"
                "After writing the file, briefly confirm that 'grounded-plan.md' was created."
            ),
        },
        "execution": {
            "label": str(labels_override.get("execution") or "Execution"),
            "agent": str(agents_override.get("execution") or AGENT_EXECUTE),
            "input_file": grounded_plan_file,
            "output_file": execution_file,
            "message": (
                "Read ONLY the attached grounded plan and execute it in the repository.\n"
                "Hard requirements:\n"
                "- At the end, save a concise execution report ONLY to 'execution.md' in the SAME DIRECTORY as the grounded plan.\n"
                "The execution report must include:\n"
                "- What files were changed (paths)\n"
                "- What endpoints/DTOs were added or updated\n"
                "- How to verify manually (curl/examples)\n"
                "- Any follow-ups or known limitations\n"
                "After writing the file, briefly confirm that 'execution.md' was created."
            ),
        },
    }
    return [{"id": s, **step_defs[s]} for s in requested_steps]


def run_opencode_step_stream(
    *,
    agent: str,
    message: str,
    files: List[Path],
    cwd: Path,
    log_path: Path,
    callback: EventCallback = None,
    control: Optional[RunControl] = None,
    title: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Tuple[int, str, Optional[str]]:
    cmd = build_cmd_base(files) + ["--agent", agent]
    if title:
        cmd += ["--title", title]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(message)

    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n$ " + " ".join(cmd) + "\n\n")
    emit(callback, {"kind": "command", "text": "$ " + " ".join(cmd)})

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    if control:
        control.set_active_proc(proc)

    raw: List[str] = []
    sess: Optional[str] = session_id
    assert proc.stdout is not None
    for line in proc.stdout:
        if control and control.stop_requested and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

        line = line.rstrip("\n")
        raw.append(line)
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            emit(callback, {"kind": "opencode_event", "event": ev})
            if sess is None:
                sess = ev.get("sessionID") or ev.get("sessionId") or ev.get("session_id")
                if sess:
                    emit(callback, {"kind": "session", "session_id": sess})
        except Exception:
            emit(callback, {"kind": "line", "text": line})

    rc = proc.wait(timeout=STEP_TIMEOUT_SEC) if STEP_TIMEOUT_SEC else proc.wait()
    if control:
        control.set_active_proc(None)
    out_all = "\n".join(raw) + "\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(out_all)

    sid = parse_session_id_from_json_events(out_all) or sess
    return rc, out_all, sid


def run_selected_task(
    task_id: Optional[str] = None,
    callback: EventCallback = None,
    control: Optional[RunControl] = None,
) -> RunResult:
    require_exists(PROJECT_DIR, "PROJECT_DIR does not exist")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    tasks = read_tasks_jsonl(TASKS_FILE)
    idx = find_task_index(tasks, task_id) if task_id else find_next_todo(tasks)
    if idx is None:
        msg = f"Task id not found: {task_id}" if task_id else "No TODO tasks."
        return RunResult(False, task_id, None, None, msg)

    task = tasks[idx]
    selected_id = str(task.get("id") or f"task{idx+1}")
    if "task_file" not in task:
        raise ValueError(f"Task {selected_id} missing 'task_file'")

    task_file = resolve_file(task["task_file"], PROJECT_DIR)
    require_exists(task_file, "Task file not found")
    task_dir = task_file.parent
    concept_plan_file = task_dir / "concept-plan.md"
    grounded_plan_file = task_dir / "grounded-plan.md"
    execution_file = task_dir / "execution.md"
    steps = build_pipeline_steps(
        task=task,
        task_file=task_file,
        concept_plan_file=concept_plan_file,
        grounded_plan_file=grounded_plan_file,
        execution_file=execution_file,
    )

    run_id = f'{datetime.now().strftime("%Y%m%d_%H%M%S")}_{selected_id}'
    log_path = RUNS_DIR / f"{run_id}.log"
    title = f"{selected_id}: {task_file.name}"
    session_id: Optional[str] = None

    emit(
        callback,
        {
            "kind": "run_start",
            "task_id": selected_id,
            "model": MODEL,
            "steps_total": len(steps),
            "log_path": str(log_path),
        },
    )

    ok = True
    if not steps:
        ok = False
        emit(callback, {"kind": "error", "text": "No steps selected"})

    for step_index, step in enumerate(steps):
        if not ok:
            break
        if control and control.stop_requested:
            ok = False
            emit(callback, {"kind": "error", "text": "Run stopped by user"})
            break

        label = str(step["label"])
        input_file: Path = step["input_file"]
        output_file: Path = step["output_file"]
        agent = str(step["agent"])

        emit(
            callback,
            {
                "kind": "step_start",
                "index": step_index,
                "label": label,
                "agent": agent,
                "total": len(steps),
            },
        )

        if not input_file.exists():
            ok = False
            emit(callback, {"kind": "error", "text": f"Missing input file: {input_file}"})
            break

        rc, _out, sid = run_opencode_step_stream(
            agent=agent,
            message=str(step["message"]),
            files=[input_file],
            cwd=PROJECT_DIR,
            log_path=log_path,
            callback=callback,
            control=control,
            title=title if step_index == 0 else None,
            session_id=session_id,
        )
        session_id = sid or session_id

        if rc != 0 or not output_file.exists():
            ok = False
            if control and control.stop_requested:
                emit(callback, {"kind": "error", "text": "Run stopped by user"})
                break
            emit(
                callback,
                {
                    "kind": "error",
                    "text": f"Step '{label}' failed or output not found: {output_file.name}",
                },
            )
            break

        emit(
            callback,
            {"kind": "step_done", "index": step_index, "label": label, "output_file": str(output_file)},
        )

    if not ok:
        task.update({"status": "failed", "last_run_log": str(log_path), "session_id": session_id})
        tasks[idx] = task
        write_tasks_jsonl(TASKS_FILE, tasks)
        emit(callback, {"kind": "run_done", "ok": False, "task_id": selected_id, "session_id": session_id})
        if control and control.stop_requested:
            return RunResult(False, selected_id, session_id, log_path, f"Task {selected_id} -> stopped")
        return RunResult(False, selected_id, session_id, log_path, f"Task {selected_id} -> failed")

    task["status"] = "done"
    task["session_id"] = session_id
    task["last_run_log"] = str(log_path)
    task["outputs"] = {str(step["id"]): str(step["output_file"]) for step in steps}
    tasks[idx] = task
    write_tasks_jsonl(TASKS_FILE, tasks)
    emit(callback, {"kind": "run_done", "ok": True, "task_id": selected_id, "session_id": session_id})
    return RunResult(True, selected_id, session_id, log_path, f"Task {selected_id} -> done")
