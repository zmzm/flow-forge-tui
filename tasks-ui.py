#!/usr/bin/env python3
"""
Tasks terminal UI.
- Top: tasks table + task details
- Bottom: live run header + events feed
"""

import threading
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pipeline_runner as runner
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static, TextArea


TASKS_FILE = runner.TASKS_FILE
FILTERS = ["all", "todo", "failed", "done"]


def steps_config_text(step_configs: List[Dict[str, Any]]) -> str:
    return json.dumps({"steps": step_configs}, ensure_ascii=False, indent=2)


def read_tasks(path: Path) -> List[Dict[str, Any]]:
    try:
        return runner.read_tasks_jsonl(path)
    except FileNotFoundError:
        return []


def write_tasks(path: Path, tasks: List[Dict[str, Any]]) -> None:
    runner.write_tasks_jsonl(path, tasks)


def fmt_agents(task: Dict[str, Any]) -> str:
    step_configs = runner.read_step_configs()
    agents = runner.task_agents(task, step_configs)
    return ", ".join(agents[step["id"]] for step in step_configs)


def status_color(status: str) -> str:
    s = status.lower()
    if s == "done":
        return "green"
    if s == "failed":
        return "red"
    if s == "todo":
        return "yellow"
    return "cyan"


class EditTaskScreen(ModalScreen[Optional[Dict[str, Any]]]):
    CSS = """
    EditTaskScreen {
        align: center middle;
    }

    #dialog {
        width: 92;
        height: 18;
        border: round #31577a;
        background: #0d1622;
        padding: 1 2;
    }

    #dialog_title {
        height: 1;
        color: #94d3ff;
        text-style: bold;
        margin-bottom: 1;
    }

    .label {
        height: 1;
        color: #a8bfdb;
    }

    .inp {
        height: 3;
        width: 1fr;
    }

    #actions {
        height: 3;
        margin-top: 1;
    }

    #hint {
        height: 3;
        color: #8ea3bc;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "save", "Save"),
    ]

    def __init__(self, task: Dict[str, Any]) -> None:
        super().__init__()
        self.task_data = task
        self.step_configs = runner.read_step_configs()

    def compose(self) -> ComposeResult:
        agents = runner.task_agents(self.task_data, self.step_configs)
        agent_values = [agents[step["id"]] for step in self.step_configs]
        step_order = ", ".join(step["id"] for step in self.step_configs)
        run_steps = runner.task_run_step_ids(self.task_data) or []

        yield Vertical(
            Static(f"EDIT TASK: {self.task_data.get('id', '-')}", id="dialog_title"),
            Static("Agents (comma-separated, in step order)", classes="label"),
            Input(value=", ".join(agent_values), id="agents", classes="inp"),
            Static("Run steps (comma-separated, empty = all/auto-resume)", classes="label"),
            Input(value=", ".join(run_steps), id="run_steps", classes="inp"),
            Horizontal(
                Button("Save", id="save", variant="success"),
                Button("Cancel", id="cancel"),
                id="actions",
            ),
            Static(f"Order: {step_order}\nFailed tasks auto-resume from first missing output when empty.\nEsc cancel | Ctrl+S save", id="hint"),
            id="dialog",
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "save":
            self._save()

    def _save(self) -> None:
        raw_agents = self.query_one("#agents", Input).value
        agent_values = [item.strip() for item in raw_agents.split(",") if item.strip()]
        if len(agent_values) != len(self.step_configs):
            self.notify(
                f"Expected {len(self.step_configs)} agents, got {len(agent_values)}",
                severity="error",
            )
            return
        raw_run_steps = self.query_one("#run_steps", Input).value
        run_steps = [item.strip() for item in raw_run_steps.split(",") if item.strip()]
        allowed_steps = {step["id"] for step in self.step_configs}
        unknown_steps = [step_id for step_id in run_steps if step_id not in allowed_steps]
        if unknown_steps:
            self.notify(f"Unknown run steps: {', '.join(unknown_steps)}", severity="error")
            return
        result = {
            "id": str(self.task_data.get("id", "")),
            "agents": {
                step["id"]: agent_values[index]
                for index, step in enumerate(self.step_configs)
            },
            "run_steps": run_steps,
        }
        if not all(result["agents"].values()):
            self.notify("Agent values cannot be empty", severity="error")
            return
        self.dismiss(result)


class AddAgentsScreen(ModalScreen[bool]):
    CSS = """
    AddAgentsScreen {
        align: center middle;
    }

    #dialog {
        width: 88;
        height: 13;
        border: round #85662b;
        background: #171306;
        padding: 1 2;
    }

    #dialog_title {
        height: 1;
        color: #ffd37a;
        text-style: bold;
        margin-bottom: 1;
    }

    #message {
        height: 5;
        color: #f4ddb0;
    }

    #actions {
        height: 3;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, additions: List[Dict[str, str]]) -> None:
        super().__init__()
        self.additions = additions

    def compose(self) -> ComposeResult:
        lines = [f"{item['label']} ({item['step_id']}): {item['agent']}" for item in self.additions]
        yield Vertical(
            Static("ADD AGENTS", id="dialog_title"),
            Static(
                "These agents are not listed in steps.json:\n"
                + "\n".join(lines)
                + "\nAdd them before saving?",
                id="message",
            ),
            Horizontal(
                Button("Add and Save", id="add", variant="warning"),
                Button("Cancel", id="cancel"),
                id="actions",
            ),
            id="dialog",
        )

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            self.dismiss(True)
            return
        if event.button.id == "cancel":
            self.dismiss(False)


class EditStepsConfigScreen(ModalScreen[Optional[List[Dict[str, Any]]]]):
    CSS = """
    EditStepsConfigScreen {
        align: center middle;
    }

    #dialog {
        width: 110;
        height: 34;
        border: round #31577a;
        background: #0d1622;
        padding: 1 2;
    }

    #dialog_title {
        height: 1;
        color: #94d3ff;
        text-style: bold;
        margin-bottom: 1;
    }

    #steps_editor {
        height: 22;
        border: tall #1b3349;
        background: #07101a;
    }

    #actions {
        height: 3;
        margin-top: 1;
    }

    #hint {
        height: 3;
        color: #8ea3bc;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "save", "Save"),
        ("ctrl+n", "add_step", "Add Step"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.step_configs = runner.read_step_configs()

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("EDIT STEPS CONFIG", id="dialog_title"),
            TextArea(
                steps_config_text(self.step_configs),
                language="json",
                id="steps_editor",
                show_line_numbers=True,
            ),
            Horizontal(
                Button("Add Step", id="add_step", variant="primary"),
                Button("Save", id="save", variant="success"),
                Button("Cancel", id="cancel"),
                id="actions",
            ),
            Static(
                "Edit steps.json directly. Required per step: id, label, agent. Optional: model, agents.\n"
                "New step ids use generic sequential execution and write <step-id>.md.\n"
                "Esc cancel | Ctrl+S save | Ctrl+N add step",
                id="hint",
            ),
            id="dialog",
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._save()

    def action_add_step(self) -> None:
        self._add_step_template()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "save":
            self._save()
            return
        if event.button.id == "add_step":
            self._add_step_template()

    def _read_editor_doc(self) -> Dict[str, Any]:
        text = self.query_one("#steps_editor", TextArea).text
        raw = json.loads(text)
        if isinstance(raw, list):
            return {"steps": raw}
        if isinstance(raw, dict):
            return raw
        raise ValueError("steps config must be a JSON object or list")

    def _save(self) -> None:
        try:
            raw = self._read_editor_doc()
            configs = runner.normalize_step_configs(raw.get("steps"), "steps editor")
        except Exception as exc:
            self.notify(str(exc), severity="error")
            return
        self.dismiss(configs)

    def _add_step_template(self) -> None:
        try:
            raw = self._read_editor_doc()
            steps = raw.get("steps")
            if not isinstance(steps, list):
                raise ValueError("steps must be a list")
        except Exception as exc:
            self.notify(str(exc), severity="error")
            return

        existing = {
            str(step.get("id", "")).strip()
            for step in steps
            if isinstance(step, dict)
        }
        next_id = "new_step"
        counter = 2
        while next_id in existing:
            next_id = f"new_step_{counter}"
            counter += 1
        steps.append({"id": next_id, "label": "New step", "agent": "new-agent", "model": ""})
        editor = self.query_one("#steps_editor", TextArea)
        editor.text = json.dumps({"steps": steps}, ensure_ascii=False, indent=2)


class TasksUI(App):
    CSS = """
    Screen {
        layout: vertical;
        background: #0a0f16;
        color: #d8e5f5;
    }

    #toolbar {
        height: 1;
        margin: 0 1;
        color: #99b4d4;
    }

    #main {
        height: 1fr;
        margin: 0 1;
    }

    #table_panel {
        width: 2fr;
        border: round #244463;
        margin-right: 1;
        padding: 0 1;
    }

    #details_panel {
        width: 1fr;
        border: round #244463;
        padding: 0 1;
        background: #0c141f;
    }

    #details_title {
        height: 1;
        color: #90d0ff;
        text-style: bold;
        margin-bottom: 1;
    }

    #events_panel {
        height: 16;
        margin: 0 1;
        border: round #2f4f6e;
        background: #0b131d;
        padding: 0 1;
    }

    #events_header {
        height: 1;
        color: #8de3ff;
        text-style: bold;
        margin-bottom: 1;
    }

    #events_feed {
        height: 1fr;
        border: tall #1b3349;
        background: #07101a;
        color: #dbe7f3;
    }

    #status {
        height: 1;
        margin: 0 1 1 1;
        color: #b8cde6;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("u", "refresh", "Refresh"),
        ("f", "cycle_filter", "Filter"),
        ("r", "run_selected", "Run"),
        ("s", "stop_run", "Stop"),
        ("e", "edit_selected", "Edit"),
        ("g", "edit_steps_config", "Config"),
        ("c", "clear_events", "Clear Feed"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.tasks: List[Dict[str, Any]] = []
        self.visible_tasks: List[Dict[str, Any]] = []
        self.filter_index = 0
        self.selected_index: Optional[int] = None

        self.runner_active = False
        self.running_task_id: Optional[str] = None
        self.run_control: Optional[runner.RunControl] = None
        self.current_model = runner.MODEL or "default"
        self.current_agent = "-"
        self.current_step = "-"
        self.current_session = "-"

        self.status_text = "Ready"
        self.spinner_frames = ["|", "/", "-", "\\"]
        self.spinner_index = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="toolbar")

        with Horizontal(id="main"):
            with Vertical(id="table_panel"):
                yield DataTable(id="tasks_table")
            with Vertical(id="details_panel"):
                yield Static("TASK DETAILS", id="details_title")
                yield Static("Select a task", id="details")

        with Vertical(id="events_panel"):
            yield Static("", id="events_header")
            yield RichLog(id="events_feed", highlight=True, wrap=True)

        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks_table", DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Status", "Agents", "Log")
        self._update_events_header()
        self.set_interval(0.15, self._tick_spinner)
        self._load_and_render()
        self._set_status("Ready")

    def action_refresh(self) -> None:
        self._load_and_render()
        self._set_status("Refreshed")

    def action_cycle_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(FILTERS)
        self._load_and_render()
        self._set_status(f"Filter: {FILTERS[self.filter_index]}")

    def action_clear_events(self) -> None:
        self.query_one("#events_feed", RichLog).clear()
        self._set_status("Events feed cleared")

    def action_run_selected(self) -> None:
        if self.runner_active:
            self._set_status("Runner is already active")
            return

        task = self._selected_task()
        if not task:
            self._set_status("No task selected")
            return

        task_id = str(task.get("id", ""))
        if not task_id:
            self._set_status("Selected task has no id")
            return

        try:
            step_configs = runner.read_step_configs()
            runner.task_agents(task, step_configs)
        except ValueError as exc:
            self._set_status(str(exc))
            return

        self._start_task_run(task_id)

    def _start_task_run(self, task_id: str) -> None:
        if self.runner_active:
            self._set_status("Runner is already active")
            return

        self.runner_active = True
        self.running_task_id = task_id
        self.run_control = runner.RunControl()
        self.current_agent = "-"
        self.current_step = "-"
        self.current_session = "-"
        self._update_events_header()
        self._push_event("[cyan]RUN[/cyan] starting " + escape(task_id), markup=True)
        self._set_status(f"Running {task_id}")

        t = threading.Thread(target=self._run_task_thread, args=(task_id,), daemon=True)
        t.start()

    def action_stop_run(self) -> None:
        if not self.runner_active:
            self._set_status("No active run")
            return
        if self.run_control:
            self.run_control.request_stop()
            self._push_event("[red]STOP[/red] requested by user", markup=True)
            self._set_status("Stopping run...")

    def action_edit_selected(self) -> None:
        task = self._selected_task()
        if not task:
            self._set_status("No task selected")
            return
        try:
            step_configs = runner.read_step_configs()
            runner.task_agents(task, step_configs)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self.push_screen(EditTaskScreen(task), self._on_edit_done)

    def action_edit_steps_config(self) -> None:
        try:
            self.push_screen(EditStepsConfigScreen(), self._on_steps_config_done)
        except ValueError as exc:
            self._set_status(str(exc))

    def _on_steps_config_done(self, result: Optional[List[Dict[str, Any]]]) -> None:
        if not result:
            self._set_status("Config edit canceled")
            return
        runner.write_step_configs(result)
        self._load_and_render()
        self._set_status("Saved steps config")

    def _on_edit_done(self, result: Optional[Dict[str, Any]]) -> None:
        if not result:
            self._set_status("Edit canceled")
            return

        try:
            step_configs = runner.read_step_configs()
            unknown_agents = runner.unknown_task_agents({"agents": result["agents"]}, step_configs)
        except ValueError as exc:
            self._set_status(str(exc))
            return

        if unknown_agents:
            self.push_screen(
                AddAgentsScreen(unknown_agents),
                lambda should_add: self._on_add_agents_done(should_add, result, unknown_agents),
            )
            return

        self._save_edit_result(result)

    def _on_add_agents_done(
        self,
        should_add: bool,
        result: Dict[str, Any],
        additions: List[Dict[str, str]],
    ) -> None:
        if not should_add:
            self._set_status("Save canceled")
            return
        runner.add_known_step_agents(additions)
        self._save_edit_result(result)

    def _save_edit_result(self, result: Dict[str, Any]) -> None:

        task_id = str(result.get("id", ""))
        if not task_id:
            self._set_status("Edited task has no id")
            return

        updated = False
        for item in self.tasks:
            if str(item.get("id", "")) == task_id:
                item["agents"] = result["agents"]
                if result["run_steps"]:
                    item["run_steps"] = result["run_steps"]
                else:
                    item.pop("run_steps", None)
                item.pop("steps", None)
                item.pop("step_labels", None)
                updated = True
                break

        if not updated:
            self._set_status(f"Task not found: {task_id}")
            return

        write_tasks(TASKS_FILE, self.tasks)
        self._load_and_render()
        self._set_status(f"Saved: {task_id}")

    def _run_task_thread(self, task_id: str) -> None:
        result = runner.run_selected_task(
            task_id=task_id,
            callback=self._event_from_runner,
            control=self.run_control,
        )

        def finalize() -> None:
            self.runner_active = False
            self.running_task_id = None
            self.run_control = None
            self._load_and_render()
            if result.ok:
                self._set_status(f"Done: {task_id}")
            else:
                self._set_status(f"Failed: {task_id}")
            self._push_event(f"[bold {'green' if result.ok else 'red'}]{escape(result.message)}[/]", markup=True)

        self.call_from_thread(finalize)

    def _event_from_runner(self, event: Dict[str, Any]) -> None:
        self.call_from_thread(self._handle_runner_event_ui, event)

    def _handle_runner_event_ui(self, event: Dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "run_start":
            self.current_model = str(event.get("model") or "default")
            self.current_step = "0/" + str(event.get("steps_total", "?"))
            self._update_events_header()
            self._push_event(f"[dim]mode: {escape(str(event.get('run_mode', 'full')))}[/dim]", markup=True)
            self._push_event(f"[dim]log: {escape(str(event.get('log_path', '-')))}[/dim]", markup=True)
            return

        if kind == "session":
            self.current_session = str(event.get("session_id") or "-")
            self._update_events_header()
            return

        if kind == "step_start":
            index = int(event.get("index", 0)) + 1
            total = int(event.get("total", 0))
            self.current_step = f"{index}/{total}"
            self.current_agent = str(event.get("agent") or "-")
            self.current_model = str(event.get("model") or runner.MODEL or "default")
            self._update_events_header()
            self._push_event(
                f"[green]STEP[/green] {escape(str(event.get('label', '-')))} [dim]({self.current_step})[/dim]",
                markup=True,
            )
            return

        if kind == "step_done":
            self._push_event(
                f"[green]OK[/green] {escape(str(event.get('label', '-')))} -> {escape(str(event.get('output_file', '-')))}",
                markup=True,
            )
            return

        if kind == "command":
            self._push_event(f"[dim]{escape(str(event.get('text', '')))}[/dim]", markup=True)
            return

        if kind == "line":
            self._push_event(escape(str(event.get("text", ""))), markup=True)
            return

        if kind == "error":
            self._push_event(f"[red]{escape(str(event.get('text', 'error')))}[/red]", markup=True)
            return

        if kind == "opencode_event":
            ev = event.get("event") or {}
            if not isinstance(ev, dict):
                return
            ev_type = str(ev.get("type") or "event")
            if ev_type == "step_start":
                self._push_event("[green]- step start -[/green]", markup=True)
            elif ev_type == "step_finish":
                self._push_event("[green]- step finish -[/green]", markup=True)
            elif ev_type == "text":
                part = ev.get("part") or {}
                txt = str(part.get("text") or "").strip()
                if txt:
                    for chunk in txt.splitlines():
                        c = chunk.strip()
                        if c:
                            self._push_event(f"[blue]LLM[/blue] {escape(c)}", markup=True)
            elif ev_type == "tool_use":
                part = ev.get("part") or {}
                tool = str(part.get("tool") or "tool")
                state = part.get("state") or {}
                st = str(state.get("status") or "unknown")
                self._push_event(f"[yellow]TOOL[/yellow] {escape(tool)} [dim]({escape(st)})[/dim]", markup=True)
                out = state.get("output")
                if isinstance(out, str) and out.strip():
                    first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
                    if first:
                        self._push_event(f"[dim]↳ {escape(first[:240])}[/dim]", markup=True)
            return

        if kind == "run_done":
            ok = bool(event.get("ok"))
            self._push_event("[bold green]DONE[/bold green]" if ok else "[bold red]FAILED[/bold red]", markup=True)
            return

    def _push_event(self, text: str, *, markup: bool = False) -> None:
        feed = self.query_one("#events_feed", RichLog)
        if markup:
            feed.write(Text.from_markup(text))
        else:
            feed.write(text)

    def _update_events_header(self) -> None:
        hdr = (
            f"model [#9fe870]{escape(self.current_model)}[/#9fe870]  "
            f"agent [#79d7ff]{escape(self.current_agent)}[/#79d7ff]  "
            f"step [#ffc887]{escape(self.current_step)}[/#ffc887]  "
            f"session [#ff87d0]{escape(self.current_session)}[/#ff87d0]"
        )
        self.query_one("#events_header", Static).update(hdr)

    def _load_and_render(self) -> None:
        self.tasks = read_tasks(TASKS_FILE)
        status_filter = FILTERS[self.filter_index]
        if status_filter == "all":
            self.visible_tasks = list(self.tasks)
        else:
            self.visible_tasks = [t for t in self.tasks if str(t.get("status", "todo")) == status_filter]

        table = self.query_one("#tasks_table", DataTable)
        table.clear()
        for task in self.visible_tasks:
            try:
                agents_text = fmt_agents(task)
            except ValueError:
                agents_text = "invalid agents"
            table.add_row(
                str(task.get("id", "-")),
                str(task.get("status", "todo")),
                agents_text,
                str(task.get("last_run_log", "-")),
            )

        if self.visible_tasks:
            idx = 0 if self.selected_index is None else min(self.selected_index, len(self.visible_tasks) - 1)
            table.move_cursor(row=idx, column=0)
            self.selected_index = idx
            self._render_details(self.visible_tasks[idx])
        else:
            self.selected_index = None
            self.query_one("#details", Static).update("No tasks in current filter")

        self.query_one("#toolbar", Static).update(
            (
                f"[b]tasks.jsonl[/b] | filter: [cyan]{status_filter}[/cyan] "
                f"| total: {len(self.tasks)} | shown: {len(self.visible_tasks)} | keys: e edit task, g config, r run, s stop"
            )
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_index = event.cursor_row
        if row_index < 0 or row_index >= len(self.visible_tasks):
            return
        self.selected_index = row_index
        self._render_details(self.visible_tasks[row_index])

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        row_index = event.coordinate.row
        if row_index < 0 or row_index >= len(self.visible_tasks):
            return
        self.selected_index = row_index
        self._render_details(self.visible_tasks[row_index])

    def _selected_task(self) -> Optional[Dict[str, Any]]:
        table = self.query_one("#tasks_table", DataTable)
        cursor_row = table.cursor_row
        if cursor_row is not None and cursor_row >= 0:
            self.selected_index = cursor_row
        if self.selected_index is None:
            return None
        if self.selected_index < 0 or self.selected_index >= len(self.visible_tasks):
            return None
        return self.visible_tasks[self.selected_index]

    def _render_details(self, task: Dict[str, Any]) -> None:
        raw_status = str(task.get("status", "todo"))
        st_color = status_color(raw_status)

        outputs = task.get("outputs", {})
        outputs_lines: List[str] = []
        if isinstance(outputs, dict) and outputs:
            for key, value in outputs.items():
                outputs_lines.append(f"[#6dd3ff]{escape(str(key))}[/#6dd3ff]: [#b8f7a4]{escape(str(value))}[/#b8f7a4]")
        else:
            outputs_lines.append("[dim]-[/dim]")

        agents_lines: List[str] = []
        try:
            step_configs = runner.read_step_configs()
            effective_agents = runner.task_agents(task, step_configs)
            run_steps = runner.task_run_step_ids(task)
            run_step_set = set(run_steps or [])
            for step in step_configs:
                key = step["id"]
                value = effective_agents[key]
                model = str(step.get("model") or runner.MODEL or "default")
                active_marker = "" if run_steps is None or key in run_step_set else " [dim](skipped)[/dim]"
                agents_lines.append(
                    f"[#6dd3ff]{escape(step['label'])}[/#6dd3ff] "
                    f"[dim]({escape(key)})[/dim]: [#ffcc8a]{escape(value)}[/#ffcc8a] "
                    f"[dim]model {escape(model)}[/dim]{active_marker}"
                )
        except ValueError as exc:
            agents_lines.append(f"[red]{escape(str(exc))}[/red]")

        text = (
            f"[#8ec5ff][b]ID[/b][/#8ec5ff]: [white]{escape(str(task.get('id', '-')))}[/white]\n"
            f"[#8ec5ff][b]STATUS[/b][/#8ec5ff]: [{st_color}]{escape(raw_status)}[/{st_color}]\n"
            f"[#8ec5ff][b]TASK FILE[/b][/#8ec5ff]: [#d7e4f2]{escape(str(task.get('task_file', '-')))}[/#d7e4f2]\n"
            "\n[#8ec5ff][b]AGENTS[/b][/#8ec5ff]:\n"
            + "\n".join(agents_lines)
            + "\n\n"
            f"[#8ec5ff][b]SESSION[/b][/#8ec5ff]: [#e3c8ff]{escape(str(task.get('session_id', '-')))}[/#e3c8ff]\n"
            f"[#8ec5ff][b]LAST LOG[/b][/#8ec5ff]: [#ffdca8]{escape(str(task.get('last_run_log', '-')))}[/#ffdca8]\n"
            "\n[#8ec5ff][b]OUTPUTS[/b][/#8ec5ff]:\n"
            + "\n".join(outputs_lines)
        )
        self.query_one("#details", Static).update(text)

    def _set_status(self, msg: str) -> None:
        self.status_text = msg
        self.query_one("#status", Static).update(msg)

    def _tick_spinner(self) -> None:
        if not self.runner_active or not self.running_task_id:
            return
        frame = self.spinner_frames[self.spinner_index]
        self.spinner_index = (self.spinner_index + 1) % len(self.spinner_frames)
        self.query_one("#status", Static).update(f"{self.status_text}  {frame}")


if __name__ == "__main__":
    TasksUI().run()
