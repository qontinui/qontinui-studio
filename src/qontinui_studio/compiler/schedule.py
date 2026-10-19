"""The overnight re-render (plan 2026-10-13 Phase 19 step 6): one task per
compile target, named ``image-compile-<target>``, in the Qontinui RUNNER's own
scheduler — never an OS cron, a systemd timer or a CI schedule.

A change to the imagery treatment, an exemplar set or a house LoRA (or a
recipe) leaves bindings stale; ``studio compile --changed`` finds exactly
those and re-renders them as a BULK job, which the image facade defers outside
its bulk window (a typed ``deferred``; the batch resumes on the next run). The
task fires nightly and spawns an agent session (a ``RemoteAgent`` task, the
same shape as the weekly pulse's) that:

1. allocates a worktree of the target's repository (``allocate-worktree.sh``),
   and one of qontinui at origin/main for the image licence manifest and
   recipes (``docker/imagegen``) — unless the task pins a manifest directory —
   so the recipe and LoRA axes are read, not UNKNOWN, and a recipe or house
   LoRA change re-renders what it touched;
2. runs ``studio compile --changed --imagegen-dir <that docker/imagegen>``
   against the target's worktree, which writes derivatives and the manifest
   there and the PR evidence beside the studio's state;
3. when anything was written, commits ONLY the publish dir, pushes and opens
   the PR with the evidence as its body — the merge train lands it like any
   other PR; when nothing was written, hands the worktree back.

Studio itself runs no git or gh; the spawned session does, as the plan's step
4 requires. The image-generation dial governs every provider call that run
makes, so ``off`` stops it before any.

Registration mirrors :mod:`qontinui_studio.pulse_schedule` (the task NAME is
the upsert key; ``--check`` reports drift; an unanswered runner is UNKNOWN).
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qontinui_studio.compiler.target import CompileTarget
from qontinui_studio.pulse_schedule import EXIT_USAGE, ScheduleError

TASK_PREFIX = "image-compile-"
#: 02:30 local, nightly: inside the facade's bulk window, where it does not
#: defer bulk work. Outside it the facade answers ``deferred`` and the batch
#: resumes on the next run.
DEFAULT_CRON = "30 2 * * *"
MAX_TURNS = 80
TIMEOUT_SECONDS = 4 * 3600
ALLOCATE = "qontinui-claude-config/scripts/allocate-worktree.sh"
# The repository holding the image licence manifest and recipes (docker/imagegen).
QONTINUI_REPO = "qontinui"


def task_name(target: CompileTarget) -> str:
    return f"{TASK_PREFIX}{target.id}"


def nightly_cron(at: str) -> str:
    """``--at HH:MM`` -> a daily 5-field cron (the runner adds seconds)."""
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", at)
    if not m:
        raise ScheduleError(EXIT_USAGE, f"--at must be HH:MM (24 h, local), got {at!r}")
    return f"{int(m.group(2))} {int(m.group(1))} * * *"


@dataclass(frozen=True)
class CompileTask:
    """The task body ``studio compile-schedule`` installs for one target."""

    target: CompileTarget
    # The studio checkout the session runs `poetry run studio` in.
    working_directory: str
    # The studio project (`--root`): providers.yaml, sites/, targets/, .studio/.
    studio_root: str
    # The directory holding the repository checkouts and qontinui-claude-config.
    workspace_root: str
    cron: str = DEFAULT_CRON
    enabled: bool = True
    # A pinned image licence manifest directory (a qontinui checkout's
    # docker/imagegen). None: the session allocates a qontinui worktree at
    # origin/main each run and reads it there.
    imagegen_dir: str | None = None

    @property
    def name(self) -> str:
        return task_name(self.target)

    @property
    def description(self) -> str:
        return (
            f"Overnight image-slot re-render for {self.target.repo} (plan "
            "2026-10-13-image-generation-and-visual-judgment, Phase 19 step 6): `studio compile "
            "--changed` in an allocated worktree, then a PR when anything changed. Managed by "
            "qontinui-studio `studio compile-schedule`; --check reports an edit made here as "
            "drift and --install reverts it."
        )

    def _allocate(self, repo_name: str) -> str:
        ws = self.workspace_root
        return shlex.join(
            [
                "bash",
                f"{ws}/{ALLOCATE}",
                "--root",
                ws,
                "--repo",
                repo_name,
                "--intent",
                f"studio compile --changed: overnight image-slot re-render for {self.target.repo}",
            ]
        )

    def prompt(self) -> str:
        t = self.target
        repo_name = t.repo.split("/", 1)[1]
        ws = self.workspace_root
        done = f"bash {ws}/{ALLOCATE} --done --path"
        report = f"{self.studio_root}/.studio/compile/{t.id}/report.json"
        evidence = f"{self.studio_root}/.studio/compile/{t.id}/evidence.md"
        if self.imagegen_dir is not None:
            imagegen = shlex.quote(self.imagegen_dir)
            manifest_step = (
                f"Step 1b: the image licence manifest is pinned at {self.imagegen_dir}; "
                "allocate nothing for it."
            )
            release_q = ""
            replaced = "<W>"
        else:
            imagegen = "<Q>/docker/imagegen"
            manifest_step = (
                f"Step 1b: run exactly once: {self._allocate(QONTINUI_REPO)} . Q is the path "
                f"on its line for {QONTINUI_REPO} (origin/main: the image licence manifest and "
                "recipes the recipe and LoRA axes are read from). If it refuses, go to the "
                "final step: without the manifest those axes are UNKNOWN."
            )
            release_q = f" Then, if Q was allocated: {done} <Q> --abandon (you never change Q)."
            replaced = "<W> and <Q>"
        compile_cmd = (
            f"poetry run --quiet studio --root {shlex.quote(self.studio_root)} compile "
            f"--target {t.id} --slots <W>/{t.slots_dir.as_posix()} --changed "
            f"--imagegen-dir {imagegen} --json"
        )
        return " ".join(
            [
                f"You re-render stale image slots of {t.repo} (studio compile target '{t.id}').",
                "Every path that stops after step 1 — a refusal, an exit code that says stop, "
                "nothing written, or the PR opened — ends with the FINAL step; never stop "
                "without it.",
                f"Step 1: run exactly once: {self._allocate(repo_name)} . It prints '<repo> "
                f"<path> <branch>' lines; W is the path and B the branch on the line for "
                f"{repo_name}. If it refuses, stop and report its message (nothing to hand back).",
                manifest_step,
                f"Step 2: from this working directory run: {compile_cmd} (with {replaced} "
                f"replaced). It writes {report} and {evidence}. Read the report's exit_code and "
                "files_written, and route on files_written FIRST: whatever the exit code, "
                "files_written true goes to step 4 (a refusal can come after slots were "
                "written). Exit 6 (image generation switched off or refused) with "
                "files_written false: go to the final step. Exit 3 = generation deferred or "
                "failed (a host outside its bulk window, or busy): the batch resumes on the "
                "next run; go to step 3. Exit 7 with slots in status awaiting_alt_text: open "
                "each such slot's derivative files listed in the report, write one factual "
                "alt text per slot describing exactly that image, save them as JSON "
                '{"<slot id>": {"asset_sha256": "<the slot pick asset_sha256>", "text": '
                '"<alt text>"}} in a file outside W, and run the step 2 command once more '
                "with --alt-texts <that file>. Leave every other status for an interactive "
                "session; do not author SVGs or change icons.",
                "Step 3: if files_written is false, go to the final step.",
                f"Step 4: in W, run git status: only files under {t.publish_dir.as_posix()} "
                "may have changed; if anything else changed, report it and go to the final "
                f"step. Stage only {t.publish_dir.as_posix()}, commit with the message "
                "'feat(images): re-render stale image slots (studio compile --changed)', push "
                "B with git push -u origin B, and open the PR with gh pr create --base main "
                f"--head B --title 'feat(images): re-render stale image slots for {t.id}' "
                f"--body-file {evidence} . Report the PR URL.",
                f"FINAL step (on every path): if you opened the PR from W, run {done} <W> ; "
                f"otherwise run {done} <W> --abandon .{release_q}",
                "Never merge the PR, never force-push, never touch a file outside W except the "
                "alt-text JSON, and never run studio compile without --changed.",
            ]
        )

    def task(self) -> dict[str, Any]:
        return {
            "task_type": "RemoteAgent",
            "prompt": self.prompt(),
            "working_directory": self.working_directory,
            "max_turns": MAX_TURNS,
            "timeout_seconds": TIMEOUT_SECONDS,
        }

    def common(self) -> dict[str, Any]:
        return {
            "name": self.name,
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


def dry_run(intended: CompileTask) -> str:
    return "\n".join(
        [
            f"create (no task named {intended.name} yet): POST /scheduler/tasks",
            json.dumps(intended.create_body(), indent=2),
            "update (an existing task): PUT /scheduler/tasks/<id>",
            json.dumps(intended.update_body(), indent=2),
        ]
    )


def allocator_present(workspace_root: Path) -> bool:
    return (workspace_root / ALLOCATE).is_file()
