#!/usr/bin/env python3
"""
Headless pipeline runner.
Contains task selection, pipeline execution, and tasks.jsonl updates.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, IO, List, Optional, Tuple


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


def _optional_int_env(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got: {raw!r}") from None
    return value if value > 0 else None


PROJECT_DIR = Path(_require_env("PROJECT_DIR")).expanduser()
TASKS_FILE = Path(_require_env("TASKS_FILE")).expanduser()
STEPS_FILE = TASKS_FILE.with_name("steps.json")
RUNS_DIR = Path(os.getenv("FLOWFORGE_RUNS_DIR", "runs")).expanduser()

DEFAULT_STEP_CONFIGS = [
    {"id": "concept", "label": "Business plan", "agent": "concept-plan"},
    {"id": "grounded", "label": "Technical plan", "agent": "grounded-hard"},
    {"id": "execution", "label": "Execution", "agent": "execution-plan"},
]
SUPPORTED_STEP_IDS = {step["id"] for step in DEFAULT_STEP_CONFIGS}
STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Models are normally selected by each OpenCode agent's frontmatter. A model in
# steps.json is an explicit per-step override; there is no global CLI override.
MODEL: Optional[str] = None
VARIANT: Optional[str] = None
USE_ATTACH = False
ATTACH_URL = "http://localhost:4096"
STEP_TIMEOUT_SEC: Optional[int] = _optional_int_env("FLOWFORGE_STEP_TIMEOUT_SECONDS", 3600)
SHARE_SESSION = False

EventCallback = Optional[Callable[[Dict[str, Any]], None]]


@dataclass
class RunResult:
    ok: bool
    task_id: Optional[str]
    session_id: Optional[str]
    log_path: Optional[Path]
    message: str
    stage: Optional[str] = None


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
    data = "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n"
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _persist_task_update(path: Path, idx: int, task_id: str, updates: Dict[str, Any]) -> None:
    """Merge a task's field updates into a freshly read tasks.jsonl (atomic)."""
    tasks = read_tasks_jsonl(path)
    pos = find_task_index(tasks, task_id)
    if pos is None and 0 <= idx < len(tasks):
        pos = idx
    if pos is None:
        tasks.append({"id": task_id, **updates})
    else:
        tasks[pos].update(updates)
    write_tasks_jsonl(path, tasks)


def recover_stale_running_tasks(path: Path) -> List[str]:
    """Mark 'running' tasks (left over from a dead process) as failed so they auto-resume."""
    try:
        tasks = read_tasks_jsonl(path)
    except (FileNotFoundError, ValueError):
        return []
    recovered: List[str] = []
    changed = False
    for task in tasks:
        if str(task.get("status", "")) == "running":
            task["status"] = "failed"
            recovered.append(str(task.get("id") or "-"))
            changed = True
    if changed:
        write_tasks_jsonl(path, tasks)
    return recovered


def acquire_lock(path: Path) -> Optional[IO[str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
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


def normalize_step_configs(raw_steps: Any, source: str = "steps config") -> List[Dict[str, Any]]:
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(f"{source} must contain a non-empty 'steps' list")

    configs: List[Dict[str, Any]] = []
    seen = set()
    defaults = {step["id"]: step for step in DEFAULT_STEP_CONFIGS}
    for index, raw_step in enumerate(raw_steps, 1):
        if isinstance(raw_step, str):
            step_id = raw_step.strip()
            default = defaults.get(step_id, {})
            label = str(default.get("label") or step_id)
            agent = str(default.get("agent") or "").strip()
        elif isinstance(raw_step, dict):
            step_id = str(raw_step.get("id", "")).strip()
            default = defaults.get(step_id, {})
            label = str(raw_step.get("label") or default.get("label") or step_id).strip()
            agent = str(raw_step.get("agent") or default.get("agent") or "").strip()
            model = str(raw_step.get("model") or default.get("model") or "").strip()
            extra_agents = raw_step.get("agents", [])
        else:
            raise ValueError(f"Invalid step config at index {index} in {source}")
        if isinstance(raw_step, str):
            extra_agents = []
            model = ""

        if not step_id:
            raise ValueError(f"Step id cannot be empty at index {index} in {source}")
        if not STEP_ID_RE.fullmatch(step_id):
            raise ValueError(f"Step id '{step_id}' in {source} must use only letters, numbers, '_' or '-'")
        if step_id in seen:
            raise ValueError(f"Duplicate step id '{step_id}' in {source}")
        if not label:
            raise ValueError(f"Step label cannot be empty for '{step_id}' in {source}")
        if not agent:
            raise ValueError(f"Step agent cannot be empty for '{step_id}' in {source}")
        if not isinstance(extra_agents, list):
            raise ValueError(f"Step agents must be a list for '{step_id}' in {source}")

        agents = sorted({str(item).strip() for item in extra_agents if str(item).strip()} - {agent})
        config = {"id": step_id, "label": label, "agent": agent}
        if model:
            config["model"] = model
        if agents:
            config["agents"] = agents
        configs.append(config)
        seen.add(step_id)

    return configs


def read_step_configs(path: Path = STEPS_FILE) -> List[Dict[str, Any]]:
    if not path.exists():
        return [dict(step) for step in DEFAULT_STEP_CONFIGS]

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_steps = raw.get("steps") if isinstance(raw, dict) else raw
    return normalize_step_configs(raw_steps, str(path))


def write_step_configs(step_configs: List[Dict[str, Any]], path: Path = STEPS_FILE) -> None:
    serializable = []
    for step in step_configs:
        item = {
            "id": str(step["id"]),
            "label": str(step["label"]),
            "agent": str(step["agent"]),
        }
        model = str(step.get("model") or "").strip()
        if model:
            item["model"] = model
        agents = sorted({str(agent).strip() for agent in step.get("agents", []) if str(agent).strip()} - {item["agent"]})
        if agents:
            item["agents"] = agents
        serializable.append(item)
    path.write_text(json.dumps({"steps": serializable}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def step_known_agents(step: Dict[str, Any]) -> List[str]:
    agents = {str(step["agent"])}
    agents.update(str(agent).strip() for agent in step.get("agents", []) if str(agent).strip())
    return sorted(agents)


def unknown_task_agents(
    task: Dict[str, Any],
    step_configs: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    configs = step_configs if step_configs is not None else read_step_configs()
    agents = task_agents(task, configs)
    unknown: List[Dict[str, str]] = []
    for step in configs:
        step_id = step["id"]
        agent = agents[step_id]
        if agent not in step_known_agents(step):
            unknown.append({"step_id": step_id, "label": step["label"], "agent": agent})
    return unknown


def add_known_step_agents(
    additions: List[Dict[str, str]],
    path: Path = STEPS_FILE,
) -> List[Dict[str, Any]]:
    configs = read_step_configs(path)
    by_id = {step["id"]: step for step in configs}
    for addition in additions:
        step = by_id.get(addition["step_id"])
        if not step:
            continue
        agent = str(addition["agent"]).strip()
        if not agent or agent == step["agent"]:
            continue
        agents = set(step.get("agents", []))
        agents.add(agent)
        step["agents"] = sorted(agents)
    write_step_configs(configs, path)
    return configs


def pipeline_step_ids(step_configs: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    configs = step_configs if step_configs is not None else read_step_configs()
    return [step["id"] for step in configs]


def task_agents(task: Dict[str, Any], step_configs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, str]:
    overrides = task.get("agents") or {}
    if not isinstance(overrides, dict):
        raise ValueError("Task field 'agents' must be an object if provided")
    return {
        step["id"]: str(overrides.get(step["id"]) or step["agent"])
        for step in (step_configs if step_configs is not None else read_step_configs())
    }


def agent_configured_model(agent: str, project_dir: Path = PROJECT_DIR) -> Optional[str]:
    """Best-effort lookup of the model declared in an OpenCode agent file."""
    candidates = [
        project_dir / ".opencode" / "agents" / f"{agent}.md",
        Path.home() / ".config" / "opencode" / "agents" / f"{agent}.md",
        Path.home() / ".opencode" / "agents" / f"{agent}.md",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter = content.split("---", 2)
        if len(frontmatter) < 3:
            continue
        match = re.search(r"(?m)^model\s*:\s*['\"]?([^'\"\s#]+)", frontmatter[1])
        if match:
            return match.group(1).strip()
    return None


def task_run_step_ids(task: Dict[str, Any]) -> Optional[List[str]]:
    raw_steps = task.get("run_steps")
    if raw_steps is None:
        return None
    if isinstance(raw_steps, str):
        steps = [item.strip() for item in raw_steps.split(",") if item.strip()]
    elif isinstance(raw_steps, list):
        steps = [str(item).strip() for item in raw_steps if str(item).strip()]
    else:
        raise ValueError("Task field 'run_steps' must be a list or comma-separated string if provided")
    if not steps:
        return None
    duplicates = sorted({step_id for step_id in steps if steps.count(step_id) > 1})
    if duplicates:
        raise ValueError(f"Duplicate run_steps: {duplicates}")
    return steps


def select_steps_for_task(task: Dict[str, Any], steps: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    requested_ids = task_run_step_ids(task)
    if requested_ids:
        by_id = {str(step["id"]): step for step in steps}
        unknown = [step_id for step_id in requested_ids if step_id not in by_id]
        if unknown:
            allowed = ", ".join(str(step["id"]) for step in steps)
            raise ValueError(f"Unknown run_steps: {unknown}. Allowed: {allowed}")
        return [by_id[step_id] for step_id in requested_ids], "explicit"

    if str(task.get("status", "todo")) != "failed":
        return steps, "full"

    for index, step in enumerate(steps):
        output_file: Path = step["output_file"]
        if not output_file.exists():
            return steps[index:], "resume"

    return [], "already_complete"


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


def opencode_bin() -> str:
    bin_path = os.getenv("OPENCODE_BIN", "").strip() or "opencode"
    if "/" in bin_path:
        bin_path = str(Path(bin_path).expanduser().resolve())
    return bin_path


def build_cmd_base(
    files: List[Path],
    model: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> List[str]:
    cmd = [opencode_bin(), "run", "--format", "json", "--print-logs", "--log-level", "ERROR"]
    if cwd:
        # OpenCode may use the inherited PWD instead of the subprocess cwd when
        # resolving the project. Be explicit so sessions belong to PROJECT_DIR.
        cmd += ["--dir", str(cwd)]
    if USE_ATTACH:
        cmd += ["--attach", ATTACH_URL]
    if model:
        cmd += ["--model", model]
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


def parse_opencode_error_from_json_events(output: str) -> Optional[str]:
    """Return the most useful error reported by OpenCode's JSON event stream."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") != "error":
            continue

        error = event.get("error")
        if not isinstance(error, dict):
            return str(error or "Unknown OpenCode error")
        data = error.get("data")
        data = data if isinstance(data, dict) else {}
        name = str(error.get("name") or "OpenCode error")
        message = str(data.get("message") or error.get("message") or "").strip()
        reference = str(data.get("ref") or error.get("ref") or "").strip()

        details = f"{name}: {message}" if message else name
        if reference:
            details += f" (ref: {reference})"
        return details
    return None


def step_output_file(step_id: str, task_dir: Path) -> Path:
    fixed_names = {
        "concept": "concept-plan.md",
        "grounded": "grounded-plan.md",
        "execution": "execution.md",
    }
    return task_dir / fixed_names.get(step_id, f"{step_id}.md")


def step_message(step_id: str, label: str, output_file: Path) -> str:
    output_name = output_file.name
    if step_id == "concept":
        return (
            "Read the attached task file and produce a CONCEPT PLAN.\n"
            "Hard requirements:\n"
            f"- Save your output ONLY to a file named '{output_name}' in the SAME DIRECTORY as the task file.\n"
            f"After writing the file, briefly confirm that '{output_name}' was created."
        )
    if step_id == "grounded":
        return (
            "Read ONLY the attached previous plan and produce a GROUNDED PLAN.\n"
            "Hard requirements:\n"
            f"- Save your output ONLY to '{output_name}' in the SAME DIRECTORY as the previous plan.\n"
            f"After writing the file, briefly confirm that '{output_name}' was created."
        )
    if step_id == "execution":
        return (
            "Read ONLY the attached previous plan and execute it in the repository.\n"
            "Hard requirements:\n"
            f"- At the end, save a concise execution report ONLY to '{output_name}' in the SAME DIRECTORY as the previous plan.\n"
            "The execution report must include:\n"
            "- What files were changed (paths)\n"
            "- What endpoints/DTOs were added or updated\n"
            "- How to verify manually (curl/examples)\n"
            "- Any follow-ups or known limitations\n"
            f"After writing the file, briefly confirm that '{output_name}' was created."
        )
    return (
        f"Read ONLY the attached input and produce this pipeline step: {label}.\n"
        "Hard requirements:\n"
        f"- Save your output ONLY to '{output_name}' in the SAME DIRECTORY as the attached input.\n"
        f"After writing the file, briefly confirm that '{output_name}' was created."
    )


def build_pipeline_steps(
    *,
    task: Dict[str, Any],
    task_file: Path,
    concept_plan_file: Path,
    grounded_plan_file: Path,
    execution_file: Path,
) -> List[Dict[str, Any]]:
    step_configs = read_step_configs()
    agents = task_agents(task, step_configs)
    task_dir = task_file.parent
    input_file = task_file
    steps: List[Dict[str, Any]] = []
    for step in step_configs:
        step_id = step["id"]
        output_file = step_output_file(step_id, task_dir)
        model_override = str(step.get("model") or "").strip() or None
        configured_model = agent_configured_model(agents[step_id])
        steps.append(
            {
                "id": step_id,
                "label": step["label"],
                "agent": agents[step_id],
                "model": model_override,
                "display_model": model_override or configured_model or "OpenCode default",
                "model_source": "step override" if model_override else ("agent" if configured_model else "OpenCode"),
                "input_file": input_file,
                "output_file": output_file,
                "message": step_message(step_id, step["label"], output_file),
            }
        )
        input_file = output_file
    return steps


def run_opencode_step_stream(
    *,
    agent: str,
    model: Optional[str],
    message: str,
    files: List[Path],
    cwd: Path,
    log_path: Path,
    callback: EventCallback = None,
    control: Optional[RunControl] = None,
    title: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Tuple[int, str, Optional[str], bool]:
    cmd = build_cmd_base(files, model=model, cwd=cwd) + ["--agent", agent]
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
        env={**os.environ, "PWD": str(cwd)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    if control:
        control.set_active_proc(proc)

    timed_out = False

    def _expire() -> None:
        nonlocal timed_out
        timed_out = True
        proc.terminate()
        kill_timer = threading.Timer(10, lambda: proc.kill() if proc.poll() is None else None)
        kill_timer.daemon = True
        kill_timer.start()

    timer = threading.Timer(STEP_TIMEOUT_SEC, _expire) if STEP_TIMEOUT_SEC else None
    if timer:
        timer.daemon = True
        timer.start()

    raw: List[str] = []
    sess: Optional[str] = session_id
    assert proc.stdout is not None
    try:
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
    finally:
        if timer:
            timer.cancel()
    if control:
        control.set_active_proc(None)
    out_all = "\n".join(raw) + "\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(out_all)

    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()

    sid = parse_session_id_from_json_events(out_all) or sess
    return rc, out_all, sid, timed_out


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
    all_steps = build_pipeline_steps(
        task=task,
        task_file=task_file,
        concept_plan_file=concept_plan_file,
        grounded_plan_file=grounded_plan_file,
        execution_file=execution_file,
    )
    steps, run_mode = select_steps_for_task(task, all_steps)

    run_id = f'{datetime.now().strftime("%Y%m%d_%H%M%S")}_{selected_id}'
    log_path = RUNS_DIR / f"{run_id}.log"
    title = f"{selected_id}: {task_file.name}"
    session_id: Optional[str] = None
    failed_stage: Optional[str] = None

    task["status"] = "running"
    _persist_task_update(TASKS_FILE, idx, selected_id, {"status": "running"})

    emit(
        callback,
        {
            "kind": "run_start",
            "task_id": selected_id,
            "model": "per-agent",
            "steps_total": len(steps),
            "run_mode": run_mode,
            "log_path": str(log_path),
        },
    )

    ok = True
    if not steps:
        if run_mode == "already_complete":
            log_path.write_text(
                f"Task {selected_id} already has outputs for all configured steps; nothing to run.\n",
                encoding="utf-8",
            )
            _persist_task_update(
                TASKS_FILE,
                idx,
                selected_id,
                {
                    "status": "done",
                    "last_run_log": str(log_path),
                    "outputs": {str(step["id"]): str(step["output_file"]) for step in all_steps},
                },
            )
            emit(callback, {"kind": "run_done", "ok": True, "task_id": selected_id, "session_id": session_id})
            return RunResult(True, selected_id, session_id, log_path, f"Task {selected_id} -> already complete")
        ok = False
        emit(callback, {"kind": "error", "text": "No steps selected"})

    try:
        for step_index, step in enumerate(steps):
            if not ok:
                break
            if control and control.stop_requested:
                ok = False
                emit(callback, {"kind": "error", "text": "Run stopped by user"})
                break

            label = str(step["label"])
            failed_stage = label
            input_file: Path = step["input_file"]
            output_file: Path = step["output_file"]
            agent = str(step["agent"])
            model = str(step.get("model") or "")

            emit(
                callback,
                {
                    "kind": "step_start",
                    "index": step_index,
                    "label": label,
                    "agent": agent,
                    "model": str(step.get("display_model") or "OpenCode default"),
                    "model_source": str(step.get("model_source") or "OpenCode"),
                    "total": len(steps),
                },
            )

            if not input_file.exists():
                ok = False
                emit(callback, {"kind": "error", "text": f"Missing input file: {input_file}"})
                break

            rc, step_output, sid, step_timed_out = run_opencode_step_stream(
                agent=agent,
                model=model,
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

            provider_error = parse_opencode_error_from_json_events(step_output)
            if rc != 0 or provider_error or not output_file.exists():
                ok = False
                if control and control.stop_requested:
                    emit(callback, {"kind": "error", "text": "Run stopped by user"})
                    break
                if step_timed_out:
                    error_text = f"Step '{label}' timed out after {STEP_TIMEOUT_SEC}s and was terminated"
                elif provider_error:
                    error_text = f"Step '{label}' failed: {provider_error}"
                elif rc != 0:
                    error_text = f"Step '{label}' failed: OpenCode exited with code {rc}"
                else:
                    error_text = f"Step '{label}' completed but did not create {output_file}"
                emit(callback, {"kind": "error", "text": error_text})
                break

            failed_stage = None
            emit(
                callback,
                {"kind": "step_done", "index": step_index, "label": label, "output_file": str(output_file)},
            )
    except BaseException:
        _persist_task_update(
            TASKS_FILE,
            idx,
            selected_id,
            {"status": "failed", "last_run_log": str(log_path), "session_id": session_id},
        )
        raise

    if not ok:
        _persist_task_update(
            TASKS_FILE,
            idx,
            selected_id,
            {"status": "failed", "last_run_log": str(log_path), "session_id": session_id},
        )
        emit(callback, {"kind": "run_done", "ok": False, "task_id": selected_id, "session_id": session_id})
        if control and control.stop_requested:
            return RunResult(False, selected_id, session_id, log_path, f"Task {selected_id} -> stopped", failed_stage)
        return RunResult(False, selected_id, session_id, log_path, f"Task {selected_id} -> failed", failed_stage)

    outputs = task.get("outputs") if isinstance(task.get("outputs"), dict) else {}
    outputs.update({str(step["id"]): str(step["output_file"]) for step in steps})
    _persist_task_update(
        TASKS_FILE,
        idx,
        selected_id,
        {"status": "done", "session_id": session_id, "last_run_log": str(log_path), "outputs": outputs},
    )
    emit(callback, {"kind": "run_done", "ok": True, "task_id": selected_id, "session_id": session_id})
    return RunResult(True, selected_id, session_id, log_path, f"Task {selected_id} -> done")
