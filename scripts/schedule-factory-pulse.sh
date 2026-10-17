#!/usr/bin/env bash
# schedule-factory-pulse.sh - register (or check / remove) the weekly factory
# pulse as ONE task, named `factory-pulse`, in the Qontinui runner's own
# scheduler (explainers plan Phase 5). Never an OS cron, systemd timer or CI
# schedule. A thin wrapper over `studio pulse schedule`; see
# src/qontinui_studio/pulse_schedule.py for the contract.
#
#   schedule-factory-pulse.sh --install     upsert by name (twice -> UNCHANGED)
#   schedule-factory-pulse.sh --check       present/absent, enabled, cron, next run, drift
#   schedule-factory-pulse.sh --uninstall   delete every task named factory-pulse
#   schedule-factory-pulse.sh --dry-run     print the request bodies; no network
# Options: --at HH:MM (default 06:30, local)  --day mon..sun (default mon)
#          --disabled  --workdir DIR (default: the primary checkout of this repo)
#          --runner URL (default $QONTINUI_RUNNER_URL, else http://127.0.0.1:9876)
#
# Exit: 0 done (--check: present), 1 --check: absent, 2 UNKNOWN (runner did
# not answer / answered non-2xx or unreadable), 3 conflict or read-back
# mismatch, 4 usage. Never stops, restarts or rebuilds the runner; never
# touches a task with any other name.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Resolve a relative --workdir against the caller's directory before moving.
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --workdir)
            [ $# -ge 2 ] || { echo "schedule-factory-pulse: --workdir needs a directory" >&2; exit 4; }
            args+=("$1" "$(cd "$2" && pwd)"); shift 2 ;;
        --workdir=*) args+=("--workdir" "$(cd "${1#*=}" && pwd)"); shift ;;
        *) args+=("$1"); shift ;;
    esac
done
cd "$here"
exec poetry run --quiet studio pulse schedule "${args[@]}"
