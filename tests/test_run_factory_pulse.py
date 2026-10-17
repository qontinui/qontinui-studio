"""scripts/run-factory-pulse.sh keeps a shared primary checkout current
(explainers plan Phase 5): on a clean `main` it fast-forwards to origin/main
and re-runs the updated script once; otherwise it says why and runs the
current tree. It never stashes, resets or rebases. Real git repositories in a
temp dir; `poetry` and `pnpm` are stubs that record their calls."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-factory-pulse.sh"


def git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.test",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_script(repo: Path, marker: str) -> None:
    """Commit the real script, tagged with a marker the stub run prints."""
    (repo / "scripts").mkdir(exist_ok=True)
    body = SCRIPT.read_text().replace(
        "exec poetry run studio", f'echo "version {marker}" >&2\nexec poetry run studio'
    )
    (repo / "scripts" / "run-factory-pulse.sh").write_text(body)
    (repo / "scripts" / "lib").mkdir(exist_ok=True)
    shutil.copyfile(
        SCRIPT.parent / "lib" / "pulse-env.sh", repo / "scripts" / "lib" / "pulse-env.sh"
    )
    (repo / "scenes").mkdir(exist_ok=True)
    (repo / "scenes" / ".keep").write_text("")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"script {marker}")


@pytest.fixture()
def setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    commit_script(seed, "one")
    git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(checkout))
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("poetry", "pnpm"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\n')
        stub.chmod(0o755)
    return seed, checkout, stubs


def run(
    checkout: Path, stubs: Path, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        PATH=f"{stubs}:{os.environ['PATH']}", STUB_LOG=str(log), GIT_CONFIG_GLOBAL="/dev/null"
    )
    # No host pulse.env leaks in from the machine running the tests.
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env.pop("STUDIO_PULSE_ENV_FILE", None)
    env.update(extra_env or {})
    done = subprocess.run(
        ["bash", str(checkout / "scripts" / "run-factory-pulse.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done, log.read_text()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_clean_main_behind_origin_fast_forwards_and_runs_the_new_script_once(
    setup: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    seed, checkout, stubs = setup
    commit_script(seed, "two")
    git(seed, "push", "-q", "origin", "main")
    done, calls = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "fast-forwarded main" in done.stderr
    assert "version two" in done.stderr and "version one" not in done.stderr
    assert git(checkout, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
    assert calls.count("poetry install") == 1, "the updated script runs once, not twice"
    assert "poetry run studio --root" in calls


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_an_up_to_date_main_runs_without_re_exec(
    setup: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, checkout, stubs = setup
    done, calls = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "fast-forwarded" not in done.stderr and "not updating" not in done.stderr
    assert "version one" in done.stderr and calls.count("poetry install") == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_dirty_tree_is_left_alone_and_the_current_tree_runs(
    setup: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    seed, checkout, stubs = setup
    commit_script(seed, "two")
    git(seed, "push", "-q", "origin", "main")
    (checkout / "notes.txt").write_text("a peer's work")
    head = git(checkout, "rev-parse", "HEAD")
    done, _ = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "uncommitted changes" in done.stderr and "version one" in done.stderr
    assert git(checkout, "rev-parse", "HEAD") == head
    assert (checkout / "notes.txt").read_text() == "a peer's work"
    assert git(checkout, "stash", "list") == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_another_branch_is_left_alone(setup: tuple[Path, Path, Path], tmp_path: Path) -> None:
    seed, checkout, stubs = setup
    commit_script(seed, "two")
    git(seed, "push", "-q", "origin", "main")
    git(checkout, "checkout", "-q", "-b", "feature")
    done, _ = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "on feature, not main" in done.stderr and "version one" in done.stderr
    assert git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "feature"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_diverged_main_is_not_rewritten(setup: tuple[Path, Path, Path], tmp_path: Path) -> None:
    seed, checkout, stubs = setup
    commit_script(seed, "two")
    git(seed, "push", "-q", "origin", "main")
    (checkout / "local.txt").write_text("local commit")
    git(checkout, "add", "local.txt")
    git(checkout, "commit", "-q", "-m", "local")
    local = git(checkout, "rev-parse", "HEAD")
    done, _ = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "fast-forward to origin/main failed" in done.stderr and "version one" in done.stderr
    assert git(checkout, "rev-parse", "HEAD") == local


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_failed_fetch_runs_the_current_tree(
    setup: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, checkout, stubs = setup
    git(checkout, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    done, _ = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "git fetch origin main failed" in done.stderr and "version one" in done.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_detached_head_is_left_alone(setup: tuple[Path, Path, Path], tmp_path: Path) -> None:
    seed, checkout, stubs = setup
    commit_script(seed, "two")
    git(seed, "push", "-q", "origin", "main")
    head = git(checkout, "rev-parse", "HEAD")
    git(checkout, "checkout", "-q", "--detach")
    done, _ = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "a detached HEAD, not main" in done.stderr and "version one" in done.stderr
    assert git(checkout, "rev-parse", "HEAD") == head


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_an_ignored_local_file_is_never_overwritten_by_the_fast_forward(
    setup: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    seed, checkout, stubs = setup
    (checkout / ".git" / "info" / "exclude").write_text("secret\n")
    (checkout / "secret").write_text("local state")
    (seed / "secret").write_text("upstream")
    git(seed, "add", "secret")
    git(seed, "commit", "-q", "-m", "track secret")
    git(seed, "push", "-q", "origin", "main")
    head = git(checkout, "rev-parse", "HEAD")
    done, _ = run(checkout, stubs, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "fast-forward to origin/main failed" in done.stderr
    assert (checkout / "secret").read_text() == "local state"
    assert git(checkout, "rev-parse", "HEAD") == head


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_an_inherited_git_dir_does_not_redirect_the_update(
    setup: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    seed, checkout, stubs = setup
    commit_script(seed, "two")
    git(seed, "push", "-q", "origin", "main")
    elsewhere = tmp_path / "elsewhere"
    git(tmp_path, "init", "-q", "-b", "other", str(elsewhere))
    done, _ = run(checkout, stubs, tmp_path, {"GIT_DIR": str(elsewhere / ".git")})
    assert done.returncode == 0, done.stderr
    assert "fast-forwarded main" in done.stderr and "version two" in done.stderr
