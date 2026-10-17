"""scripts/lib/pulse-env.sh: the host-local pulse.env the scheduled pulse reads
its speech service URLs from (explainers plan Phase 5)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "pulse-env.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Under the same shell options as run-factory-pulse.sh, so a `set -u` slip in
# the library fails here too.
PROBE = (
    'set -euo pipefail; . "$1"; rc=0; pulse_env_load || rc=$?; '
    'echo "rc=$rc tts=${STUDIO_TTS_URL-<unset>} asr=${STUDIO_ASR_URL-<unset>}"'
)
# Exactly how run-factory-pulse.sh calls it.
SCRIPT_SHAPE = 'set -euo pipefail; . "$1"; pulse_env_load || exit 3; echo "loaded"'


def run(
    tmp_path: Path, env: dict[str, str], probe: str = PROBE, check: bool = True
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("STUDIO_", "XDG_"))}
    base["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        ["bash", "-c", probe, "probe", str(LIB)],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=check,
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_absent_default_file_is_fine_and_sets_nothing(tmp_path: Path) -> None:
    r = run(tmp_path, {})
    assert r.stdout.strip() == "rc=0 tts=<unset> asr=<unset>"
    assert "using the environment only" in r.stderr  # says where it looked


def test_a_file_with_only_comments_loads_nothing(tmp_path: Path) -> None:
    f = write(tmp_path / "p.env", "# nothing yet\n\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(f)})
    assert r.stdout.strip() == "rc=0 tts=<unset> asr=<unset>"
    assert "holds no settings" in r.stderr


def test_the_script_exits_3_on_a_refused_file_and_runs_on_a_good_one(tmp_path: Path) -> None:
    bad = write(tmp_path / "bad.env", "PATH=/evil\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(bad)}, probe=SCRIPT_SHAPE, check=False)
    assert (r.returncode, r.stdout) == (3, "")
    good = write(tmp_path / "good.env", "STUDIO_TTS_URL=http://h:1\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(good)}, probe=SCRIPT_SHAPE, check=False)
    assert (r.returncode, r.stdout.strip()) == (0, "loaded")
    r = run(tmp_path, {}, probe=SCRIPT_SHAPE, check=False)  # absent default file
    assert (r.returncode, r.stdout.strip()) == (0, "loaded")


def test_default_file_under_xdg_config_is_loaded(tmp_path: Path) -> None:
    write(
        tmp_path / "cfg" / "qontinui-studio" / "pulse.env",
        "# this host's speech services\n\nSTUDIO_TTS_URL=http://127.0.0.1:8880\r\nSTUDIO_ASR_URL=http://127.0.0.1:8890",
    )
    r = run(tmp_path, {"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    assert r.stdout.strip() == "rc=0 tts=http://127.0.0.1:8880 asr=http://127.0.0.1:8890"
    assert "STUDIO_TTS_URL STUDIO_ASR_URL" in r.stderr
    assert "8880" not in r.stderr  # keys are reported, values never


def test_home_config_is_the_fallback(tmp_path: Path) -> None:
    write(
        tmp_path / "home" / ".config" / "qontinui-studio" / "pulse.env",
        "STUDIO_TTS_URL=http://h:1\n",
    )
    assert run(tmp_path, {}).stdout.strip() == "rc=0 tts=http://h:1 asr=<unset>"


def test_environment_wins_over_the_file(tmp_path: Path) -> None:
    f = write(tmp_path / "p.env", "STUDIO_TTS_URL=http://file:1\nSTUDIO_ASR_URL=http://file:2\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(f), "STUDIO_TTS_URL": "http://env:9"})
    assert r.stdout.strip() == "rc=0 tts=http://env:9 asr=http://file:2"


def test_an_empty_environment_value_does_not_shadow_the_file(tmp_path: Path) -> None:
    f = write(tmp_path / "p.env", "STUDIO_TTS_URL=http://file:1\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(f), "STUDIO_TTS_URL": ""})
    assert r.stdout.strip() == "rc=0 tts=http://file:1 asr=<unset>"


def test_the_first_occurrence_of_a_key_wins(tmp_path: Path) -> None:
    f = write(tmp_path / "p.env", "STUDIO_TTS_URL=\nSTUDIO_TTS_URL=http://second:2\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(f)})
    assert r.stdout.strip() == "rc=0 tts= asr=<unset>"  # even an empty first value


def test_value_is_data_not_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    f = write(tmp_path / "p.env", f"STUDIO_TTS_URL=$(touch {marker})\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(f)})
    assert not marker.exists()
    assert f"tts=$(touch {marker})" in r.stdout


@pytest.mark.parametrize(
    "line",
    [
        "PATH=/evil",
        "export STUDIO_TTS_URL=http://x",
        "STUDIO_tts=http://x",
        "not a setting",
        "LD_PRELOAD=/x.so",
    ],
)
def test_any_other_line_refuses_the_whole_file(tmp_path: Path, line: str) -> None:
    f = write(tmp_path / "p.env", f"STUDIO_ASR_URL=http://ok:1\n{line}\n")
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(f)})
    assert r.stdout.strip() == "rc=3 tts=<unset> asr=<unset>"  # nothing applied
    assert "line 2" in r.stderr


def test_a_named_file_that_is_missing_is_refused(tmp_path: Path) -> None:
    r = run(tmp_path, {"STUDIO_PULSE_ENV_FILE": str(tmp_path / "nope.env")})
    assert r.stdout.strip().startswith("rc=3")
    assert "does not exist" in r.stderr
