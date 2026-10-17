# shellcheck shell=bash
# pulse-env.sh - sourced by run-factory-pulse.sh. Loads this host's pulse
# settings (the speech service URLs, above all) from a host-local file, since
# the runner-scheduler task that runs the pulse inherits the runner's
# environment and nothing else.
#
#   file: $STUDIO_PULSE_ENV_FILE, else ${XDG_CONFIG_HOME:-$HOME/.config}/qontinui-studio/pulse.env
#
# The file is DATA, never sourced as shell: each line is blank, a `#` comment,
# or KEY=VALUE with KEY matching STUDIO_[A-Z0-9_]+ (no quotes are stripped, no
# expansion is done). A variable already set to a NON-EMPTY value in the
# environment wins over the file; the first occurrence of a key in the file
# wins over a later one. Any other line refuses the whole file (exit 3 from
# the caller), so a typo never silently drops a setting.
#
# The file is trusted like a shell profile: STUDIO_FFMPEG / STUDIO_FFPROBE name
# binaries that get run. Keep it owner-writable only (0600 if it names a key).
#
# An absent file is fine: the pulse then runs with what the environment has
# (with no STUDIO_TTS_URL it renders silent and the gate blocks it, recorded).
#
# Defines pulse_env_load; returns 0 loaded or absent, 3 refused. Reports the
# KEYS it set (never values) on stderr.

pulse_env_load() {
    local file="${STUDIO_PULSE_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/qontinui-studio/pulse.env}"
    if [ ! -e "$file" ]; then
        if [ -n "${STUDIO_PULSE_ENV_FILE:-}" ]; then
            echo "run-factory-pulse: STUDIO_PULSE_ENV_FILE names $file, which does not exist" >&2
            return 3
        fi
        echo "run-factory-pulse: no $file; using the environment only" >&2
        return 0
    fi
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then
        echo "run-factory-pulse: $file is not a readable file" >&2
        return 3
    fi
    local line key n=0 set_keys=""
    local -a keys=() values=()
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        line="${line%$'\r'}"
        # Blank (whitespace only) or comment.
        if [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi
        if [[ ! "$line" =~ ^(STUDIO_[A-Z0-9_]+)=(.*)$ ]]; then
            echo "run-factory-pulse: $file line $n is not STUDIO_*=value; refusing the file" >&2
            return 3
        fi
        keys+=("${BASH_REMATCH[1]}")
        values+=("${BASH_REMATCH[2]}")
    done <"$file"
    local i
    for i in "${!keys[@]}"; do
        key="${keys[$i]}"
        case " $set_keys " in *" $key "*) continue ;; esac  # first occurrence wins
        if [ -z "${!key:-}" ]; then
            export "$key=${values[$i]}"
            set_keys="$set_keys $key"
        fi
    done
    if [ "${#keys[@]}" -eq 0 ]; then
        echo "run-factory-pulse: $file holds no settings" >&2
    else
        echo "run-factory-pulse: from $file:${set_keys:- (nothing; the environment already sets every key)}" >&2
    fi
    return 0
}
