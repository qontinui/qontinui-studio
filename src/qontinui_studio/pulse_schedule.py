"""Register the weekly factory pulse as ONE task, named ``factory-pulse``, in
the Qontinui RUNNER's own scheduler (explainers plan Phase 5).

Scheduling happens inside Qontinui — never an OS cron, a systemd timer or a CI
schedule. This module upserts a task through the runner's HTTP API
(``GET/POST /scheduler/tasks``, ``PUT/DELETE /scheduler/tasks/{id}``, default
``http://127.0.0.1:9876``); the runner persists it, evaluates it on its tick
and catches up one missed run after a start. It mirrors the fleet's
``qontinui-claude-config/scripts/lib/runner-scheduler.sh`` contract:

- the task NAME is the upsert key; two tasks with that name is a conflict;
- ``--check`` reports present/absent, enabled, the cron, the next run and any
  drift from the intended body; ``--install`` puts the intended body back;
- an unanswered or unreadable runner is UNKNOWN, never "absent".

The task is a ``RemoteAgent`` task whose prompt starts ONE command,
``scripts/run-factory-pulse.sh``, in the background, waits for it and reports
its exit code and log tail. (qontinui-schemas declares a ``Script`` task type,
but no runner build dispatches it yet; when one does, this becomes a Script
task running the command directly.) That command renders,
gates and writes drafts under ``.studio/pulse/``; it posts nothing.

Exit codes (``studio pulse schedule``): 0 done (``--check``: the task exists),
1 ``--check``: no such task, 2 UNKNOWN (runner unreachable / non-2xx /
unreadable), 3 conflict (two tasks of the name, or a write that does not read
back), 4 usage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

TASK_NAME = "factory-pulse"
DEFAULT_RUNNER = "http://127.0.0.1:9876"
#: Monday 06:30 local: early in the week, after the week it reports on has ended.
#: Day-of-week by NAME: the runner's cron numbers days 1-7 from Sunday, so a
#: numeric "1" (Monday in classic cron) fires on Sunday there. Measured
#: 2026-10-17: "30 6 * * 1" gave nextRun Sunday 2026-10-18 04:30Z.
DEFAULT_CRON = "30 6 * * Mon"
MAX_TURNS = 40
TIMEOUT_SECONDS = 5400

EXIT_OK, EXIT_ABSENT, EXIT_UNKNOWN, EXIT_CONFLICT, EXIT_USAGE = 0, 1, 2, 3, 4

_CRON = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")


class ScheduleError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def pulse_prompt(command: str) -> str:
    """The agent task's prompt. A render can outlast a single tool call, so
    the session starts the script detached (nohup + setsid, so it survives
    the tool call that started it) and waits with one exact, bounded command."""
    start = (
        "mkdir -p .studio && rm -f .studio/pulse-run.log && "
        f"nohup setsid sh -c '{command} > .studio/pulse-run.log 2>&1; "
        'echo "exit $?" >> .studio/pulse-run.log\' > /dev/null 2>&1 &'
    )
    wait = "timeout 540 sh -c 'until grep -Eq \"^exit [0-9]+$\" .studio/pulse-run.log; do sleep 20; done'"
    return (
        f"From this working directory, run this exactly once, as written: {start} . "
        f"Then run this wait command, repeating it while it exits 124 (it can take up to 90 minutes in all): {wait} . "
        "When it exits 0, report the 'exit N' line and the last 20 lines of .studio/pulse-run.log, and nothing else. "
        "The script produces last week's Qontinui factory pulse: it refreshes this checkout's dependencies "
        "(poetry, pnpm, the headless browser), then renders, runs the publish gate and writes a review page "
        "and draft posts under .studio/pulse/. It posts nothing anywhere. exit 0 = the pulse ran (outcome "
        "ready, blocked or no-export, each honest and recorded in pulse.json); exit 1 = the pulse failed; any "
        "other code = a setup step failed. You change no files yourself; do not start the script twice, do not "
        "retry, post nothing, open no PR, register no gate."
    )


@dataclass(frozen=True)
class Intended:
    """The task body this module installs."""

    working_directory: str
    cron: str = DEFAULT_CRON
    enabled: bool = True

    @property
    def command(self) -> str:
        return "bash scripts/run-factory-pulse.sh"

    @property
    def description(self) -> str:
        return (
            "Weekly Qontinui factory pulse (plan 2026-10-09-qontinui-marketing-studio-animated-process-explainers, "
            "Phase 5): renders, gates and drafts posts; posts nothing. Managed by qontinui-studio "
            "`studio pulse schedule`; --check reports an edit made here as drift and --install reverts it."
        )

    def task(self) -> dict[str, Any]:
        return {
            "task_type": "RemoteAgent",
            "prompt": pulse_prompt(self.command),
            "working_directory": self.working_directory,
            "max_turns": MAX_TURNS,
            "timeout_seconds": TIMEOUT_SECONDS,
        }

    def common(self) -> dict[str, Any]:
        return {
            "name": TASK_NAME,
            "description": self.description,
            "schedule": {"type": "Cron", "value": self.cron},
            "task": self.task(),
            "skipIfCompleted": False,
            "autoFixOnFailure": False,
            "catchUpPolicy": "run_once",
        }

    def create_body(self) -> dict[str, Any]:
        return self.common()

    def update_body(self) -> dict[str, Any]:
        return {**self.common(), "enabled": self.enabled}


def cron_for(at: str, day: str) -> str:
    """``--at HH:MM --day mon`` -> a weekly 5-field cron (the runner adds seconds)."""
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", at)
    days = {
        "sun": "Sun",
        "mon": "Mon",
        "tue": "Tue",
        "wed": "Wed",
        "thu": "Thu",
        "fri": "Fri",
        "sat": "Sat",
    }
    if not m:
        raise ScheduleError(EXIT_USAGE, f"--at must be HH:MM (24 h, local), got {at!r}")
    if day not in days:
        raise ScheduleError(EXIT_USAGE, f"--day must be one of {', '.join(days)}, got {day!r}")
    return f"{int(m.group(2))} {int(m.group(1))} * * {days[day]}"


class RunnerScheduler:
    def __init__(self, base: str = DEFAULT_RUNNER, http: httpx.Client | None = None) -> None:
        self.base = base.rstrip("/")
        # The runner's /health has been sampled at 10 s on a loaded box; size for that tail.
        self.http = http or httpx.Client(timeout=20.0)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = self.base + path
        try:
            resp = self.http.request(method, url, json=body)
        except httpx.HTTPError as e:
            raise ScheduleError(
                EXIT_UNKNOWN, f"UNKNOWN -- the runner did not answer {method} {url}: {e}"
            ) from e
        if resp.status_code not in (200, 201, 204):
            raise ScheduleError(
                EXIT_UNKNOWN,
                f"UNKNOWN -- {method} {url} answered HTTP {resp.status_code}: {resp.text[:240]}",
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError as e:
            raise ScheduleError(
                EXIT_UNKNOWN, f"UNKNOWN -- {method} {url} answered a non-JSON body"
            ) from e
        if isinstance(body, dict) and body.get("success") is False:
            raise ScheduleError(
                EXIT_UNKNOWN,
                f"UNKNOWN -- {method} {url} answered success:false: {json.dumps(body)[:240]}",
            )
        return body

    def named(self) -> list[dict[str, Any]]:
        body = self._call("GET", "/scheduler/tasks")
        if (
            not isinstance(body, dict)
            or body.get("success") is not True
            or not isinstance(body.get("data"), list)
        ):
            raise ScheduleError(
                EXIT_UNKNOWN,
                "UNKNOWN -- GET /scheduler/tasks answered without the runner's {success, data: [...]} envelope",
            )
        return [t for t in body["data"] if isinstance(t, dict) and t.get("name") == TASK_NAME]

    def one(self) -> dict[str, Any] | None:
        tasks = self.named()
        if len(tasks) > 1:
            ids = ", ".join(str(t.get("id")) for t in tasks)
            raise ScheduleError(
                EXIT_CONFLICT,
                f"{len(tasks)} tasks are named {TASK_NAME} ({ids}); remove the extras with --uninstall and --install again",
            )
        return tasks[0] if tasks else None

    def create(self, body: dict[str, Any]) -> None:
        self._call("POST", "/scheduler/tasks", body)

    def update(self, task_id: str, body: dict[str, Any]) -> None:
        self._call("PUT", f"/scheduler/tasks/{task_id}", body)

    def delete(self, task_id: str) -> None:
        self._call("DELETE", f"/scheduler/tasks/{task_id}")


def drift(task: dict[str, Any], intended: Intended) -> list[str]:
    """How the stored task differs from the intended body (empty: none)."""
    out: list[str] = []
    want = intended.update_body()
    for key in (
        "description",
        "schedule",
        "skipIfCompleted",
        "autoFixOnFailure",
        "catchUpPolicy",
        "enabled",
    ):
        if task.get(key) != want[key]:
            out.append(f"{key} is {json.dumps(task.get(key))}, intended {json.dumps(want[key])}")
    raw = task.get("task")
    stored: dict[str, Any] = raw if isinstance(raw, dict) else {}
    for key in sorted(set(stored) - set(want["task"])):
        out.append(f"task.{key} is set ({json.dumps(stored[key])[:120]}), intended absent")
    for key, value in want["task"].items():
        if stored.get(key) != value:
            out.append(
                f"task.{key} is {json.dumps(stored.get(key))[:120]}, intended {json.dumps(value)[:120]}"
            )
    return out


def install(scheduler: RunnerScheduler, intended: Intended) -> str:
    task = scheduler.one()
    if task is None:
        scheduler.create(intended.create_body())
        task = scheduler.one()
        if task is None:
            raise ScheduleError(
                EXIT_CONFLICT,
                "MISMATCH -- the create was accepted but no task named factory-pulse reads back",
            )
        if not intended.enabled:
            scheduler.update(str(task["id"]), {"enabled": False})
            task = scheduler.one()
        verb = "CREATED"
    else:
        if not drift(task, intended):
            return f"UNCHANGED id={task.get('id')} enabled={task.get('enabled')} cron={intended.cron} next_run={task.get('nextRun')}"
        scheduler.update(str(task["id"]), intended.update_body())
        task = scheduler.one()
        verb = "UPDATED"
    if task is None or drift(task, intended):
        left = drift(task, intended) if task else ["the task is gone"]
        raise ScheduleError(
            EXIT_CONFLICT,
            f"MISMATCH -- after the write the task reads back with drift: {'; '.join(left)}",
        )
    return f"{verb} id={task.get('id')} enabled={task.get('enabled')} cron={intended.cron} next_run={task.get('nextRun')}"


def check(scheduler: RunnerScheduler, intended: Intended) -> tuple[int, str]:
    task = scheduler.one()
    if task is None:
        return (
            EXIT_ABSENT,
            f"ABSENT -- no task named {TASK_NAME} (fix: studio pulse schedule --install)",
        )
    lines = [
        f"PRESENT id={task.get('id')}",
        f"  enabled:  {task.get('enabled')}",
        f"  schedule: {json.dumps(task.get('schedule'))}",
        f"  next_run: {task.get('nextRun')}",
    ]
    found = drift(task, intended)
    lines += [f"  DRIFT: {d}" for d in found] or ["  drift: none"]
    return EXIT_OK, "\n".join(lines)


def uninstall(scheduler: RunnerScheduler) -> str:
    tasks = scheduler.named()
    for t in tasks:
        scheduler.delete(str(t["id"]))
    if scheduler.named():
        raise ScheduleError(
            EXIT_CONFLICT, "MISMATCH -- a task named factory-pulse is still listed after the delete"
        )
    return f"REMOVED {len(tasks)} task(s) named {TASK_NAME}"


def dry_run(intended: Intended) -> str:
    return "\n".join(
        [
            f"create (no task named {TASK_NAME} yet): POST /scheduler/tasks",
            json.dumps(intended.create_body(), indent=2),
            "update (an existing task): PUT /scheduler/tasks/<id>",
            json.dumps(intended.update_body(), indent=2),
        ]
    )


def default_working_directory() -> Path:
    """The qontinui-studio checkout this module lives in."""
    return Path(__file__).resolve().parents[2]
