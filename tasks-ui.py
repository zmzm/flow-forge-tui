#!/usr/bin/env python3
"""
Tasks terminal UI.
- Top: tasks table + task details
- Bottom: live run header + events feed
"""

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pipeline_runner as runner
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static


TASKS_FILE = runner.TASKS_FILE
FILTERS = ["all", "todo", "failed", "done"]
ALLOWED_STEP_IDS = ["concept", "grounded", "execution"]


def read_tasks(path: Path) -> List[Dict[str, Any]]:
    try:
        return runner.read_tasks_jsonl(path)
    except FileNotFoundError:
        return []


def write_tasks(path: Path, tasks: List[Dict[str, Any]]) -> None:
    runner.write_tasks_jsonl(path, tasks)


def fmt_steps(task: Dict[str, Any]) -> str:
    step_labels = task.get("step_labels") or {}
    steps = task.get("steps") or []
    if not isinstance(steps, list):
        return "-"
    labels = [str(step_labels.get(s) or s) for s in steps]
    return ", ".join(labels) if labels else "-"


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
    #dialog {
        width: 92;
        height: 24;
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
        margin-top: 1;
    }

    .inp {
        height: 1;
    }

    #actions {
        height: 3;
        margin-top: 1;
    }

    #hint {
        height: 1;
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

    def compose(self) -> ComposeResult:
        agents = self.task_data.get("agents") or {}
        labels = self.task_data.get("step_labels") or {}
        steps = self.task_data.get("steps") or ["concept", "grounded", "execution"]

        yield Vertical(
            Static(f"EDIT TASK: {self.task_data.get('id', '-')}", id="dialog_title"),
            Static("Steps (comma-separated ids: concept, grounded, execution)", classes="label"),
            Input(value=", ".join(str(s) for s in steps), id="steps", classes="inp"),
            Static("Agent: concept", classes="label"),
            Input(value=str(agents.get("concept", "concept-plan")), id="agent_concept", classes="inp"),
            Static("Agent: grounded", classes="label"),
            Input(value=str(agents.get("grounded", "grounded-plan")), id="agent_grounded", classes="inp"),
            Static("Agent: execution", classes="label"),
            Input(value=str(agents.get("execution", "execution")), id="agent_execution", classes="inp"),
            Static("Label: concept", classes="label"),
            Input(value=str(labels.get("concept", "Concept planing")), id="label_concept", classes="inp"),
            Static("Label: grounded", classes="label"),
            Input(value=str(labels.get("grounded", "Technical analysis")), id="label_grounded", classes="inp"),
            Static("Label: execution", classes="label"),
            Input(value=str(labels.get("execution", "Implementation")), id="label_execution", classes="inp"),
            Horizontal(
                Button("Save", id="save", variant="success"),
                Button("Cancel", id="cancel"),
                id="actions",
            ),
            Static("Esc cancel | Ctrl+S save", id="hint"),
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
        raw_steps = self.query_one("#steps", Input).value
        steps = [item.strip() for item in raw_steps.split(",") if item.strip()]
        bad = [s for s in steps if s not in ALLOWED_STEP_IDS]
        if not steps:
            self.notify("Steps cannot be empty", severity="error")
            return
        if bad:
            self.notify(f"Unknown steps: {', '.join(bad)}", severity="error")
            return

        result = {
            "steps": steps,
            "agents": {
                "concept": self.query_one("#agent_concept", Input).value.strip(),
                "grounded": self.query_one("#agent_grounded", Input).value.strip(),
                "execution": self.query_one("#agent_execution", Input).value.strip(),
            },
            "step_labels": {
                "concept": self.query_one("#label_concept", Input).value.strip(),
                "grounded": self.query_one("#label_grounded", Input).value.strip(),
                "execution": self.query_one("#label_execution", Input).value.strip(),
            },
        }
        if not all(result["agents"].values()):
            self.notify("Agent values cannot be empty", severity="error")
            return
        if not all(result["step_labels"].values()):
            self.notify("Step labels cannot be empty", severity="error")
            return
        self.dismiss(result)


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
        table.add_columns("ID", "Status", "Steps", "Agents", "Log")
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
        self.push_screen(EditTaskScreen(task), self._on_edit_done)

    def _on_edit_done(self, result: Optional[Dict[str, Any]]) -> None:
        if not result:
            self._set_status("Edit canceled")
            return

        task = self._selected_task()
        if not task:
            self._set_status("Task no longer selected")
            return

        task_id = str(task.get("id", ""))
        if not task_id:
            self._set_status("Selected task has no id")
            return

        updated = False
        for item in self.tasks:
            if str(item.get("id", "")) == task_id:
                item["steps"] = result["steps"]
                item["agents"] = result["agents"]
                item["step_labels"] = result["step_labels"]
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
            agents = task.get("agents") or {}
            agents_text = ", ".join(str(v) for v in agents.values()) if isinstance(agents, dict) and agents else "-"
            table.add_row(
                str(task.get("id", "-")),
                str(task.get("status", "todo")),
                fmt_steps(task),
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
                f"| total: {len(self.tasks)} | shown: {len(self.visible_tasks)} | keys: e edit, r run, s stop, c clear"
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

        agents = task.get("agents", {})
        agents_lines: List[str] = []
        if isinstance(agents, dict) and agents:
            for key, value in agents.items():
                agents_lines.append(f"[#6dd3ff]{escape(str(key))}[/#6dd3ff]: [#ffcc8a]{escape(str(value))}[/#ffcc8a]")
        else:
            agents_lines.append("[dim]-[/dim]")

        text = (
            f"[#8ec5ff][b]ID[/b][/#8ec5ff]: [white]{escape(str(task.get('id', '-')))}[/white]\n"
            f"[#8ec5ff][b]STATUS[/b][/#8ec5ff]: [{st_color}]{escape(raw_status)}[/{st_color}]\n"
            f"[#8ec5ff][b]TASK FILE[/b][/#8ec5ff]: [#d7e4f2]{escape(str(task.get('task_file', '-')))}[/#d7e4f2]\n"
            f"[#8ec5ff][b]STEPS[/b][/#8ec5ff]: [#a6e3ff]{escape(fmt_steps(task))}[/#a6e3ff]\n"
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
