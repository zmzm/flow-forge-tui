#!/usr/bin/env python3
"""CLI wrapper for headless pipeline runner (single task or scheduled batch)."""

from __future__ import annotations

import argparse
import logging
import sys

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_CONFIG = 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", dest="task_id", help="Run specific task id instead of next TODO")
    parser.add_argument(
        "--max-tasks",
        dest="max_tasks",
        type=int,
        help="Process up to N tasks this run (overrides FLOWFORGE_MAX_TASKS_PER_RUN)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        import headless_runner
        from notifications import build_notifier

        config = headless_runner.batch_config_from_env(max_tasks_override=args.max_tasks)
        notifier = build_notifier()
    except (RuntimeError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    lock = headless_runner.acquire_lock(
        headless_runner.pr.RUNS_DIR / ".flowforge.lock"
    )
    if lock is None:
        headless_runner.logger.warning("Another FlowForge run is active; exiting.")
        return EXIT_OK

    try:
        outcome = headless_runner.run_batch(config, notifier, task_id=args.task_id)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    finally:
        headless_runner.release_lock(lock)

    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
