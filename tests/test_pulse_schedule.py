"""Registering the weekly pulse in the runner's own scheduler (explainers plan
Phase 5), against an in-process fake of the runner's /scheduler/tasks API."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from qontinui_studio import pulse_schedule as ps
from qontinui_studio.cli import main


class FakeRunner:
    """The runner's scheduler routes: tasks stored as the runner stores them
    (the runner adds id/nextRun/enabled and may reorder keys)."""

    def __init__(
        self, tasks: list[dict[str, Any]] | None = None, *, down: bool = False, status: int = 200
    ) -> None:
        self.tasks = tasks or []
        self.down = down
        self.status = status
        self.writes: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.status != 200:
            return httpx.Response(self.status, text="boom")
        path = request.url.path
        if request.method == "GET" and path == "/scheduler/tasks":
            return httpx.Response(200, json={"success": True, "data": self.tasks})
        if request.method == "POST" and path == "/scheduler/tasks":
            body = json.loads(request.content)
            task = {
                "id": str(uuid.uuid4()),
                "enabled": True,
                "nextRun": "2026-10-19T04:30:00+00:00",
                **body,
            }
            self.tasks.append(task)
            self.writes.append(("POST", task["id"]))
            return httpx.Response(201, json={"success": True, "data": task})
        task_id = path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            for t in self.tasks:
                if t["id"] == task_id:
                    t.update(json.loads(request.content))
            self.writes.append(("PUT", task_id))
            return httpx.Response(200, json={"success": True})
        if request.method == "DELETE":
            self.tasks = [t for t in self.tasks if t["id"] != task_id]
            self.writes.append(("DELETE", task_id))
            return httpx.Response(204)
        return httpx.Response(404)

    def client(self) -> ps.RunnerScheduler:
        return ps.RunnerScheduler(
            "http://runner.test", httpx.Client(transport=httpx.MockTransport(self.handle))
        )


INTENDED = ps.Intended("/work/qontinui-studio")


def test_install_creates_once_then_is_unchanged() -> None:
    runner = FakeRunner([{"id": "other", "name": "findings-steward", "enabled": True}])
    out = ps.install(runner.client(), INTENDED)
    assert out.startswith("CREATED id=")
    assert ps.install(runner.client(), INTENDED).startswith("UNCHANGED")
    assert [w[0] for w in runner.writes] == ["POST"]
    task = next(t for t in runner.tasks if t["name"] == "factory-pulse")
    assert task["schedule"] == {"type": "Cron", "value": "30 6 * * 1"}
    assert task["task"]["task_type"] == "RemoteAgent"
    assert "scripts/run-factory-pulse.sh" in task["task"]["prompt"]
    assert "posts nothing" in task["task"]["prompt"]
    assert task["task"]["working_directory"] == "/work/qontinui-studio"
    assert task["catchUpPolicy"] == "run_once" and task["skipIfCompleted"] is False
    # A task of another name is never touched.
    assert any(t["id"] == "other" for t in runner.tasks)


def test_an_edit_made_in_the_runner_is_drift_and_install_reverts_it() -> None:
    runner = FakeRunner()
    ps.install(runner.client(), INTENDED)
    task = runner.tasks[0]
    task["schedule"] = {"type": "Cron", "value": "0 0 * * *"}
    code, report = ps.check(runner.client(), INTENDED)
    assert code == ps.EXIT_OK
    assert "DRIFT: schedule" in report
    assert ps.install(runner.client(), INTENDED).startswith("UPDATED")
    assert "drift: none" in ps.check(runner.client(), INTENDED)[1]


def test_disabled_install_and_check() -> None:
    runner = FakeRunner()
    ps.install(runner.client(), ps.Intended("/w", enabled=False))
    assert runner.tasks[0]["enabled"] is False
    assert [w[0] for w in runner.writes] == ["POST", "PUT"]


def test_check_absent_and_uninstall() -> None:
    runner = FakeRunner()
    assert ps.check(runner.client(), INTENDED)[0] == ps.EXIT_ABSENT
    ps.install(runner.client(), INTENDED)
    assert ps.uninstall(runner.client()) == "REMOVED 1 task(s) named factory-pulse"
    assert ps.check(runner.client(), INTENDED)[0] == ps.EXIT_ABSENT


def test_two_tasks_of_the_name_is_a_conflict() -> None:
    runner = FakeRunner(
        [{"id": "a", "name": "factory-pulse"}, {"id": "b", "name": "factory-pulse"}]
    )
    with pytest.raises(ps.ScheduleError) as e:
        ps.install(runner.client(), INTENDED)
    assert e.value.code == ps.EXIT_CONFLICT
    assert runner.writes == []


@pytest.mark.parametrize("fake", [FakeRunner(down=True), FakeRunner(status=500)])
def test_an_unanswered_or_failing_runner_is_unknown_never_absent(fake: FakeRunner) -> None:
    with pytest.raises(ps.ScheduleError) as e:
        ps.check(fake.client(), INTENDED)
    assert e.value.code == ps.EXIT_UNKNOWN
    assert str(e.value).startswith("UNKNOWN")


def test_a_body_without_the_envelope_is_unknown() -> None:
    client = ps.RunnerScheduler(
        "http://runner.test",
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))),
    )
    with pytest.raises(ps.ScheduleError) as e:
        client.named()
    assert e.value.code == ps.EXIT_UNKNOWN


def test_cron_for() -> None:
    assert ps.cron_for("06:30", "mon") == "30 6 * * 1"
    assert ps.cron_for("23:05", "sun") == "5 23 * * 0"
    for at, day in (("6:30", "mon"), ("24:00", "mon"), ("06:30", "monday")):
        with pytest.raises(ps.ScheduleError):
            ps.cron_for(at, day)


def test_cli_dry_run_and_usage() -> None:
    runner = CliRunner()
    dry = runner.invoke(
        main, ["pulse", "schedule", "--dry-run", "--workdir", "/w", "--at", "07:15", "--day", "tue"]
    )
    assert dry.exit_code == 0
    assert '"value": "15 7 * * 2"' in dry.output
    assert runner.invoke(main, ["pulse", "schedule"]).exit_code == ps.EXIT_USAGE
    assert (
        runner.invoke(main, ["pulse", "schedule", "--dry-run", "--at", "25:00"]).exit_code
        == ps.EXIT_USAGE
    )


def test_a_task_stored_in_the_runners_real_shape_is_unchanged() -> None:
    """The runner sorts keys and adds its own fields; that is not drift."""
    body = INTENDED.create_body()
    stored = {
        "autoFixOnFailure": False,
        "catchUpGraceSeconds": 300,
        "catchUpPolicy": "run_once",
        "consecutiveLaunchFailures": 0,
        "createdAt": "2026-10-17T08:00:00+00:00",
        "description": body["description"],
        "enabled": True,
        "id": "t1",
        "launchFailureBackoffSeconds": 60,
        "modifiedAt": "2026-10-17T08:00:00+00:00",
        "name": "factory-pulse",
        "nextRun": "2026-10-19T04:30:00+00:00",
        "schedule": {"type": "Cron", "value": "30 6 * * 1"},
        "skipIfCompleted": False,
        "task": dict(sorted(body["task"].items())),
    }
    runner = FakeRunner([stored])
    assert ps.install(runner.client(), INTENDED).startswith("UNCHANGED")
    assert runner.writes == []
    assert "drift: none" in ps.check(runner.client(), INTENDED)[1]


def test_an_extra_stored_task_field_is_drift() -> None:
    runner = FakeRunner()
    ps.install(runner.client(), INTENDED)
    runner.tasks[0]["task"]["model"] = "some-model"
    assert "task.model is set" in ps.check(runner.client(), INTENDED)[1]


def test_a_write_answered_success_false_is_unknown() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": []})
        return httpx.Response(200, json={"success": False, "error": "nope"})

    client = ps.RunnerScheduler(
        "http://runner.test", httpx.Client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(ps.ScheduleError) as e:
        ps.install(client, INTENDED)
    assert e.value.code == ps.EXIT_UNKNOWN


def test_the_prompt_runs_the_script_in_the_background_and_changes_nothing_itself() -> None:
    prompt = INTENDED.task()["prompt"]
    assert "nohup setsid sh -c 'bash scripts/run-factory-pulse.sh > .studio/pulse-run.log" in prompt
    assert "^exit [0-9]+$" in prompt and "timeout 540" in prompt
    assert "do not start the script twice" in prompt and "You change no files yourself" in prompt
    assert INTENDED.task()["max_turns"] >= 30
