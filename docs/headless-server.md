# FlowForge Headless Server

How to run FlowForge autonomously on a Linux home server (e.g. a Raspberry Pi 5)
without the TUI: a systemd timer triggers `run-task.py`, which processes one or
more queued tasks and sends Matrix notifications about the task lifecycle.

```
systemd timer
      │
      ▼
FlowForge headless runner (run-task.py + headless_runner.py)
      │                                            │
      ▼                                            ▼
pipeline_runner.py ────── OpenCode ──────►  Matrix notifications
                             │                (started/done/failed/summary)
                             ├── Serena MCP (stdio, started by OpenCode itself)
                             ▼
                      project repository
```

The TUI (`python3 tasks-ui.py`) keeps working against the same `tasks.jsonl`
and is unaffected by the headless setup. Serena is configured on the OpenCode
side; FlowForge does not start, stop, or expose it — it only launches OpenCode
with `PROJECT_DIR` as the working directory, so OpenCode/Serena detect the
target project themselves.

## 1. Dependencies

- Python 3.10+ (stdlib only for the headless path; no new packages)
- `opencode` installed on the server and callable by the service user
- The TUI additionally needs `textual` and `rich` (`pip install textual rich`)
- Matrix notifications use the plain HTTP client-server API via `urllib` — no
  Matrix SDK is installed

## 2. Server installation

```bash
sudo -i
apt update && apt install -y python3 git  # as needed
git clone https://github.com/zmzm/flow-forge-tui /opt/flow-forge-tui
cd /opt/flow-forge-tui
sudo -u YOUR_USER python3 -m unittest discover -s tests   # sanity check
```

## 3. `.env` configuration

```bash
cd /opt/flow-forge-tui
cp .env.example .env
chmod 600 .env
nano .env
```

Required: `PROJECT_DIR` and `TASKS_FILE` (same values the TUI uses).
`steps.json` is expected next to `TASKS_FILE`. Optional batch settings:

| Variable | Default | Meaning |
|---|---|---|
| `FLOWFORGE_MAX_TASKS_PER_RUN` | `1` | Max tasks processed per invocation (`1` = legacy single-task behavior) |
| `FLOWFORGE_STOP_ON_FAILURE` | `true` | Stop the batch when a task fails; `false` = record and continue |
| `FLOWFORGE_MAX_RUNTIME_SECONDS` | `14400` | Do not *start* another task after this many seconds; `0` = unlimited. A running task is never killed because of this limit |
| `FLOWFORGE_STEP_TIMEOUT_SECONDS` | `3600` | Kill a single hung OpenCode step (SIGTERM, then SIGKILL after 10s) and mark the task failed; `0` = unlimited |
| `FLOWFORGE_RUNS_DIR` | `runs` | Directory for run logs and the lock file (relative to the working directory unless absolute) |
| `OPENCODE_BIN` | `opencode` | Explicit path to the opencode executable |
| `MATRIX_ENABLED` | `false` | Enable Matrix notifications |
| `MATRIX_HOMESERVER` / `MATRIX_ROOM_ID` / `MATRIX_ACCESS_TOKEN` | — | Matrix settings (required when enabled) |
| `MATRIX_TIMEOUT_SECONDS` | `10` | HTTP timeout for Matrix requests |

`.env` is gitignored. Never commit it or the access token.

Exit codes of `run-task.py`: `0` = run completed (also when the queue is empty
or another run holds the lock), `1` = one or more tasks failed, `2` =
configuration/startup error (missing env, unreadable `tasks.jsonl`, unknown
`--task-id`, Matrix enabled but misconfigured). A Matrix *delivery* failure
never changes the task result or the exit code.

## 4. OpenCode requirements

OpenCode must be installed and authenticated for the service user (run
`opencode` once interactively as that user if login is required). The scheduled
process does **not** read `~/.bashrc`/`~/.profile`, so do not rely on a
PATH extended there. Either:

- set `OPENCODE_BIN=/home/YOUR_USER/.local/bin/opencode` (recommended,
  deterministic), or
- extend `Environment=PATH=` in the service file.

FlowForge launches OpenCode with `cwd=PROJECT_DIR` (and `--dir`), which is also
what makes OpenCode pick up the correct project (and later Serena MCP).

## 5. Headless manual execution

```bash
cd /opt/flow-forge-tui
python3 run-task.py                 # next task with status todo
python3 run-task.py --task-id f2-d8 # a specific task
python3 run-task.py --max-tasks 3   # override FLOWFORGE_MAX_TASKS_PER_RUN
```

Manual runs use the same batch limits, locking, and notifications as scheduled
runs. Zero queued tasks prints `No TODO tasks.` and exits 0 silently (no
notification noise).

## 6. Matrix setup

1. Create a Matrix account for the bot / your homeserver.
2. Get its access token (Element: Settings → Help & About, or the login API).
3. Invite the account to the room you want notifications in and note the room
   id (looks like `!xxxxxxxx:example.com`; ensure the bot may post).
4. Fill `MATRIX_HOMESERVER`, `MATRIX_ROOM_ID`, `MATRIX_ACCESS_TOKEN` in `.env`
   and set `MATRIX_ENABLED=true`.

You will get, at most: one message per task started, one per task
completed/failed, and one summary per scheduled run. Error messages are capped
at 500 characters and full output stays in the server logs. If Matrix is down,
FlowForge logs the failure and continues normally.

## 7. systemd service installation

```bash
sudo cp deploy/flowforge.service /etc/systemd/system/
sudo nano /etc/systemd/system/flowforge.service   # replace YOUR_USER and /opt/flow-forge-tui
sudo systemctl daemon-reload
```

`Type=oneshot` with `TimeoutStartSec=0` lets a long batch finish; concurrent
invocations are impossible anyway thanks to the lock file (see below).

## 8. systemd timer installation (daily at 03:00)

```bash
sudo cp deploy/flowforge.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flowforge.timer
```

`Persistent=true` starts the job after boot if the scheduled moment was missed
while the machine was off — important on a Pi that may be powered down at
03:00.

## 9. Checking status

```bash
systemctl status flowforge.timer
systemctl status flowforge.service
systemctl list-timers flowforge.timer
```

## 10. Viewing logs

The runner logs to stdout/stderr, so everything lands in the journal:

```bash
journalctl -u flowforge.service            # last runs
journalctl -u flowforge.service -f         # follow a running batch
journalctl -u flowforge.service --since today
```

Per-task OpenCode transcripts remain in `runs/<timestamp>_<task_id>.log`
(relative to the working directory) and the path is stored in each task's
`last_run_log`. Secrets (`MATRIX_ACCESS_TOKEN`, API keys) are never logged.

## 11. Manually triggering a run

```bash
sudo systemctl start flowforge.service     # production service, in the journal
# or, without systemd:
sudo -u YOUR_USER /usr/bin/python3 /opt/flow-forge-tui/run-task.py
```

## 12. Disabling scheduled execution

```bash
sudo systemctl disable --now flowforge.timer
```

## Concurrency (single-instance lock)

Before doing any work, `run-task.py` takes an exclusive `flock` on
`runs/.flowforge.lock` (created automatically; location follows
`FLOWFORGE_RUNS_DIR`). If another FlowForge process holds it, the new
invocation logs a warning and exits 0 without touching the queue — two workers
can never select the same `todo` task. The lock is released automatically when
the process exits, even after a crash. The TUI takes the same lock while one of
its runs is in flight, so a UI run and a scheduled run also cannot overlap.

Additional safety nets:

- `tasks.jsonl` is rewritten atomically (temp file + `os.replace`), so a crash
  or power loss can never leave a half-written queue. Final status updates are
  merged into a freshly re-read file, so edits made while a task was running
  are not clobbered.
- While a task executes it is marked `running` in `tasks.jsonl`. If the
  process dies, the next headless run converts stale `running` tasks to
  `failed` (logged), which makes them auto-resume from the first missing step
  output instead of re-running the whole pipeline.
- Each OpenCode step is bounded by `FLOWFORGE_STEP_TIMEOUT_SECONDS` (default
  1h): a hung process gets SIGTERM, then SIGKILL, and the task is marked
  failed. As an optional hard cap for the whole service you can also set
  `RuntimeMaxSec=6h` (for example) in `flowforge.service`.

## Testing this setup

```bash
python3 -m unittest discover -s tests -v   # unit tests (Matrix fully mocked)
python3 run-task.py                        # manual smoke run
sudo systemctl start flowforge.service && journalctl -u flowforge.service -f
```
