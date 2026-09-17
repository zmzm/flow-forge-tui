# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

FlowForge TUI is a Python terminal task manager (Textual UI) for running a multi-agent AI pipeline via `opencode` CLI. Tasks live in a `tasks.jsonl` file; each run executes pipeline steps (Business plan -> Technical plan -> Execution), streams JSON events, and persists results back to `tasks.jsonl`.

## Commands

```bash
# Run the TUI (requires env vars, see below)
python3 tasks-ui.py

# Headless run: next task with status "todo"
python3 run-task.py

# Headless run: specific task
python3 run-task.py --task-id f2-d8

# Install dependencies
pip install textual rich
```

There is no linter config or `pyproject.toml`/`requirements.txt`. Python 3.10+; stdlib only plus `textual` and `rich`. `mypy` may be available on the system but is not configured. When asked to verify, at minimum run `python3 -m py_compile tasks-ui.py pipeline_runner.py run-task.py headless_runner.py` and the test suite `python3 -m unittest discover -s tests` (stdlib `unittest`; tests set their own `PROJECT_DIR`/`TASKS_FILE` env in `tests/_bootstrap.py`, never contact the network).

## Required Environment

`pipeline_runner.py` raises at import time if these are missing (no fallbacks):

- `PROJECT_DIR` — target repository where pipeline steps run (cwd for `opencode`)
- `TASKS_FILE` — path to `tasks.jsonl`

A `.env` file in the repo root is loaded automatically (simple `KEY=value` parsing, `os.environ.setdefault`). `.env`, `runs/`, and `tasks.jsonl` are gitignored — never commit them.

Note: `STEPS_FILE` is derived as `steps.json` next to `TASKS_FILE`; `RUNS_DIR` is a relative `runs/` dir in the current working directory. Logs are written to `runs/<timestamp>_<task_id>.log`.

## Architecture

Flat modules, no packaging:

- `tasks-ui.py` — Textual app. Tasks table + details panel + live events feed. Modal screens: `EditTaskScreen` (agents + run steps), `AddAgentsScreen`, `EditStepsConfigScreen`. Runner executes in a daemon thread; events are marshaled to the UI via `call_from_thread`. Entry point: `TasksUI`.
- `pipeline_runner.py` — headless engine. Task selection, step config normalization, `opencode run --format json` subprocess streaming, session-id/error parsing, `tasks.jsonl` persistence. Also the shared config/data layer the UI imports (all file reads/writes go through it).
- `run-task.py` — argparse CLI: `--task-id` (single task), `--max-tasks` (batch override). Exit codes: 0 ok / 1 task failure / 2 config error. Takes the `runs/.flowforge.lock` flock before doing work.
- `headless_runner.py` — batch loop around `run_selected_task` (`FLOWFORGE_MAX_TASKS_PER_RUN`, `FLOWFORGE_STOP_ON_FAILURE`, `FLOWFORGE_MAX_RUNTIME_SECONDS`), lifecycle logging (`flowforge.runner`), single-instance flock, notification dispatch. Notifications never affect task status or exit codes.
- `notifications/` — `base.py` (`Notifier` ABC, `NullNotifier`, `SafeNotifier` that swallows+logs failures, `parse_bool`), `matrix.py` (`MatrixNotifier` over stdlib `urllib`, no SDK), `__init__.py` (`build_notifier` from `MATRIX_*` env; enabled-but-incomplete config raises ValueError -> exit 2).

Support files:

- `steps.json` — pipeline step definitions (`id`, `label`, `agent`, optional `model` and `agents` list). Lives next to `tasks.jsonl`.
- `fake-tasks.jsonl` — demo/sample data only, not used by the code.
- `assets/` — README screenshot.
- `deploy/` — example systemd service/timer for the headless server; `docs/headless-server.md` — server deployment guide.
- `tests/` — stdlib `unittest` suite; `_bootstrap.py` forces isolated `PROJECT_DIR`/`TASKS_FILE` before any project import; Matrix HTTP is always mocked.

## Key Behaviors and Invariants

- **Model selection**: models come from the OpenCode agent's frontmatter (`agent_configured_model` searches `.opencode/agents/` and `~/.config/opencode/agents/`). A `model` in `steps.json` is an explicit per-step override. There is deliberately no global model/CLI override (`MODEL`, `VARIANT`, `USE_ATTACH`, `SHARE_SESSION` are vestigial module globals — keep behavior unless asked to change it).
- **Step selection** (`select_steps_for_task`): task `run_steps` (explicit) > full run for non-failed tasks > auto-resume for `failed` tasks, starting from the first step whose output file is missing.
- **Built-in step ids** (`concept`, `grounded`, `execution`) have fixed output filenames (`concept-plan.md`, `grounded-plan.md`, `execution.md` in the task file's directory) and specialized prompts in `step_message`. New/custom step ids run generically and write `<step-id>.md`. Each step's input is the previous step's output; the first step's input is `task_file`.
- **Step success** requires: exit code 0, no `type: "error"` event in the JSON stream, and the output file existing. Any failure marks the task `failed`; success marks `done` and merges `outputs` (step id -> path).
- **Persistence**: `read_tasks_jsonl`/`write_tasks_jsonl` rewrite the whole file; every line is a JSON object. Update `tasks.jsonl` only through these helpers.
- **Legacy task fields** `steps` and `step_labels` are obsolete; the UI editor removes them on save. Do not reintroduce them.
- **Step id validation**: ids must match `^[A-Za-z0-9_-]+$`, be unique, and have non-empty label/agent (`normalize_step_configs`).
- **Unknown agents**: the UI prompts to append unknown agents to a step's optional `agents` list in `steps.json` (`add_known_step_agents`) before saving a task edit.

## Conventions

- Single-file modules, flat repo, no comments-heavy style: keep code concise; existing code uses type hints and dataclasses (`RunResult`). Follow the existing style; do not add dependencies.
- Runner ↔ UI contract is the event dict stream (`kind`: `run_start`, `session`, `step_start`, `step_done`, `command`, `line`, `error`, `opencode_event`, `run_done`). Keep payloads backward compatible when touching `pipeline_runner.py`.
- Rich markup in the UI is escaped with `rich.markup.escape` for all dynamic text; preserve that.
