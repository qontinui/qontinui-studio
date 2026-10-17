#!/usr/bin/env bash
# run-factory-pulse.sh - what the runner-scheduler task `factory-pulse` runs
# (explainers plan Phase 5). Brings this checkout's dependencies up to date
# (idempotent: poetry, the scenes package, the pinned headless browser), then
# produces last week's pulse:
#
#   studio --root "${STUDIO_PULSE_ROOT:-<this checkout>}" pulse run "$@"
#
# The pulse renders, runs the publish gate and writes a review page and draft
# posts under <root>/.studio/pulse/<week>/. It posts nothing: social accounts
# and the final publish click are operator-owned.
#
# Host settings (STUDIO_TTS_URL, STUDIO_ASR_URL, ...) come from the
# environment, then from a host-local pulse.env (scripts/lib/pulse-env.sh says
# where and what it accepts); the environment wins.
#
# Exit: the pulse's own (0 = it ran: ready, blocked or no-export; 1 = failed),
# or the failing setup step's (2 = a linked scenes/node_modules was refused,
# 3 = the pulse.env file was refused).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

# shellcheck source=scripts/lib/pulse-env.sh
. "$here/scripts/lib/pulse-env.sh"
pulse_env_load || exit 3

poetry install --no-interaction --no-ansi --quiet
(
    cd scenes
    # A linked node_modules is a shared tree; never install through it. (The
    # reverse also holds: agent worktrees may link THEIR node_modules to this
    # checkout's. --frozen-lockfile keeps an install here from changing what
    # they see while the lockfile is unchanged.)
    if [ -L node_modules ]; then
        echo "run-factory-pulse: scenes/node_modules is a link into another checkout; refusing to install through it" >&2
        exit 2
    fi
    pnpm install --frozen-lockfile --silent
    pnpm -s browser:ensure
)
exec poetry run studio --root "${STUDIO_PULSE_ROOT:-$here}" pulse run "$@"
