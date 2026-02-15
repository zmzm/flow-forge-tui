#!/usr/bin/env python3
"""CLI wrapper for headless pipeline runner."""

import argparse

from pipeline_runner import run_selected_task


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", dest="task_id", help="Run specific task id instead of next TODO")
    args = parser.parse_args()

    result = run_selected_task(task_id=args.task_id)
    print(result.message)
    if result.log_path:
        print(f"Log: {result.log_path}")
    if result.session_id:
        print(f"Session: {result.session_id}")


if __name__ == "__main__":
    main()
