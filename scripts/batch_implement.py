#!/usr/bin/env python3
"""Batch-implement all ready SDD tasks sequentially.

Usage:
    batch_implement.py [PROJECT_DIR] [--retries N] [--recover] [TASK_ID ...]

Examples:
    batch_implement.py ~/projects/freight-erp
    batch_implement.py ~/projects/freight-erp --retries 2
    batch_implement.py ~/projects/freight-erp TMS-013 TMS-010
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        sys.exit("Python 3.11+ required (or install tomli: pip install tomli)")


IMPLEMENT_PROMPT = """\
Read ~/.claude/commands/implement.md and execute its instructions for task {task_id}.
This is the project directory. Follow the implement.md workflow exactly.\
"""


def load_config(project_dir: Path) -> dict:
    config_path = project_dir / ".tasks.toml"
    if not config_path.exists():
        sys.exit(f"No .tasks.toml found in {project_dir}")
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def get_task_ids(directory: Path) -> list[str]:
    """Extract sorted task IDs from .md files in a directory."""
    tasks = []
    for f in sorted(directory.glob("*.md")):
        parts = f.stem.split("-", 2)
        if len(parts) >= 2:
            tasks.append(f"{parts[0]}-{parts[1]}")
    return tasks


def find_task_file(directory: Path, task_id: str) -> Path | None:
    for f in directory.glob(f"{task_id}-*.md"):
        return f
    return None


def move_task(src_dir: Path, dst_dir: Path, task_id: str) -> bool:
    task_file = find_task_file(src_dir, task_id)
    if task_file and task_file.exists():
        shutil.move(str(task_file), dst_dir / task_file.name)
        return True
    return False


def recover_stuck(tasks_dir: Path) -> None:
    in_progress = tasks_dir / "4-in-progress"
    ready = tasks_dir / "3-ready"
    stuck = get_task_ids(in_progress)
    if not stuck:
        return
    log(f"⚠ Stuck tasks in 4-in-progress: {', '.join(stuck)}")
    for task_id in stuck:
        move_task(in_progress, ready, task_id)
        log(f"  ↩ {task_id} → 3-ready")
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=tasks_dir.parent,
        capture_output=True,
    )


def log(msg: str) -> None:
    print(msg, flush=True)


def implement_task(task_id: str, project_dir: Path, log_path: Path) -> bool:
    prompt = IMPLEMENT_PROMPT.format(task_id=task_id)
    with open(log_path, "w") as logfile:
        proc = subprocess.Popen(
            [
                "claude",
                "-p",
                prompt,
                "--dangerously-skip-permissions",
                "--verbose",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=project_dir,
            text=True,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            sys.stdout.write(line)
            logfile.write(line)
        proc.wait()
    return proc.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-implement ready SDD tasks")
    parser.add_argument(
        "project_dir", nargs="?", default=".", help="Project root (default: cwd)"
    )
    parser.add_argument(
        "task_ids", nargs="*", help="Specific task IDs (default: all ready)"
    )
    parser.add_argument(
        "--retries", type=int, default=1, help="Retries per task (default: 1)"
    )
    parser.add_argument(
        "--recover",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-recover stuck in-progress tasks (default: true)",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    config = load_config(project_dir)
    tasks_dir = project_dir / config["tasks"]["dir"]

    # --- Lock ---
    lock_path = tasks_dir / ".batch-implement.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another batch-implement is already running (lock held)")
    lock_file.write(f"pid={os.getpid()}\nstarted={datetime.now().isoformat()}\n")
    lock_file.flush()

    try:
        _run(args, project_dir, tasks_dir)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        lock_path.unlink(missing_ok=True)


def _run_task_with_retries(
    task_id: str,
    project_dir: Path,
    in_progress_dir: Path,
    ready_dir: Path,
    log_dir: Path,
    timestamp: str,
    retries: int,
) -> bool:
    """Run a single task with retries. Returns True on success."""
    log(f"{'━' * 40}")
    log(f"▶ {task_id} ({datetime.now():%H:%M:%S})")
    log(f"{'━' * 40}")

    for attempt in range(1 + retries):
        if attempt > 0:
            log(f"  ↻ retry {attempt}/{retries}")
            move_task(in_progress_dir, ready_dir, task_id)
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=project_dir,
                capture_output=True,
            )

        log_path = log_dir / f"{task_id}-{timestamp}-attempt{attempt}.log"
        if implement_task(task_id, project_dir, log_path):
            return True
    return False


def _print_summary(
    passed: list[str],
    failed: list[tuple[str, str]],
    start_time: datetime,
    log_dir: Path,
) -> None:
    """Print final report and send terminal notification."""
    elapsed = datetime.now() - start_time
    hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
    minutes = remainder // 60

    log(f"{'━' * 40}")
    log(f"=== Done ({datetime.now():%H:%M:%S}) ===")
    log(f"Duration: {hours}h {minutes}m")
    log(f"Passed:   {', '.join(passed) or 'none'}")
    if failed:
        log(f"Failed:   {', '.join(f'{tid} ({r})' for tid, r in failed)}")
    log(f"Logs:     {log_dir}/")

    if not failed:
        print("\033]0;✅ BATCH DONE\007\a", end="", flush=True)
    elif passed:
        print("\033]0;⚠️ BATCH PARTIAL\007\a", end="", flush=True)
    else:
        print("\033]0;❌ BATCH FAILED\007\a", end="", flush=True)


def _run(args: argparse.Namespace, project_dir: Path, tasks_dir: Path) -> None:
    ready_dir = tasks_dir / "3-ready"
    in_progress_dir = tasks_dir / "4-in-progress"
    log_dir = tasks_dir / ".batch-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if args.recover:
        recover_stuck(tasks_dir)

    tasks = args.task_ids if args.task_ids else get_task_ids(ready_dir)
    if not tasks:
        log("No ready tasks found.")
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    start_time = datetime.now()

    log(f"=== Batch Implementation ===")
    log(f"Project: {project_dir}")
    log(f"Tasks:   {', '.join(tasks)}")
    log(f"Retries: {args.retries}")
    log(f"Started: {start_time:%H:%M:%S}")
    log("")

    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    for task_id in tasks:
        if _run_task_with_retries(
            task_id, project_dir, in_progress_dir, ready_dir,
            log_dir, timestamp, args.retries,
        ):
            log(f"✅ {task_id} done\n")
            passed.append(task_id)
        else:
            reason = f"failed after {1 + args.retries} attempts"
            log(f"❌ {task_id} {reason}\n")
            failed.append((task_id, reason))

    _print_summary(passed, failed, start_time, log_dir)


if __name__ == "__main__":
    main()
