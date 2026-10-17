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
# Keeping current: the scheduled run happens in a SHARED primary checkout that
# nothing guarantees is current. So first, when this checkout is on
# `main` with a clean tree, it fetches origin/main and fast-forwards to it
# (`git merge --ff-only`), then re-runs the updated script once. On another
# branch, with a dirty tree, or when the fetch or fast-forward fails, it says
# why and carries on with the current tree. It never stashes, resets or
# rebases: someone else's work in a shared checkout is not its to move.
#
# Exit: the pulse's own (0 = it ran: ready, blocked or no-export; 1 = failed),
# or the failing setup step's (2 = a linked scenes/node_modules was refused,
# 3 = the pulse.env file was refused).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"
# An inherited GIT_DIR / GIT_WORK_TREE would make git answer about another repo.
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_NAMESPACE GIT_CEILING_DIRECTORIES

note() { printf 'run-factory-pulse: %s\n' "$*" >&2; }

keep_current() {
    local branch status before after
    branch="$(git symbolic-ref --short -q HEAD || true)"
    if [ "$branch" != main ]; then
        note "not updating: the checkout is on ${branch:-a detached HEAD}, not main; running the current tree"
        return
    fi
    if ! status="$(git status --porcelain)"; then
        note "not updating: git status failed; running the current tree"
        return
    fi
    if [ -n "$status" ]; then
        note "not updating: the tree has uncommitted changes ($(printf '%s\n' "$status" | grep -c '') paths); running the current tree"
        return
    fi
    # Bounded, and never prompting: an unattended run must not hang on the network.
    if ! GIT_TERMINAL_PROMPT=0 timeout 120 git fetch --quiet origin \
        '+refs/heads/main:refs/remotes/origin/main'; then
        note "not updating: git fetch origin main failed or timed out; running the current tree"
        return
    fi
    before="$(git rev-parse HEAD)"
    local err
    # --no-overwrite-ignore: an ignored file in a shared checkout may be a
    # peer's local state; a fast-forward that would replace one is refused.
    if ! err="$(git merge --ff-only --no-overwrite-ignore --quiet origin/main 2>&1)"; then
        note "not updating: fast-forward to origin/main failed ($(printf '%s' "$err" | tr '\n' ' ' | cut -c1-200)); running the current tree"
        return
    fi
    after="$(git rev-parse HEAD)"
    if [ "$before" != "$after" ]; then
        if [ ! -f "$here/scripts/run-factory-pulse.sh" ]; then
            note "fast-forwarded main ${before:0:9} -> ${after:0:9}, but the update removed this script; running the current process"
            return
        fi
        note "fast-forwarded main ${before:0:9} -> ${after:0:9}; running the updated script"
        RUN_FACTORY_PULSE_UPDATED=1 exec bash "$here/scripts/run-factory-pulse.sh" "$@"
    fi
}

# Once per run: the re-executed script does not update again.
if [ -z "${RUN_FACTORY_PULSE_UPDATED:-}" ]; then
    keep_current "$@"
fi

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
