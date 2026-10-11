#!/bin/sh
# Entry point of the studio HyperFrames render image.
#
#   studio-hf-render <name> <hyperframes render args…>
#
# HyperFrames writes its working directories (work-*, .<name>.hf-transaction-*)
# next to its --output. So the render goes to the container's tmpfs
# (/tmp/studio-out/<name>), which vanishes with the container, and only the
# finished artifact is copied to the /output mount: first as .partial-<name>,
# then renamed to <name> in one step. An interrupted render therefore leaves
# at most <name> or .partial-<name> (a file, or a directory of plain files) in
# /output, which the adapter removes without any recursive delete.
#
# Deadline: the render runs under `timeout`, so a container nobody manages
# any more (its client killed, the CLI gone) still ends. The adapter sets
# STUDIO_HF_DEADLINE_SECONDS to its own render timeout plus a margin; the
# default bounds a container started some other way. On expiry HyperFrames
# gets SIGTERM, then SIGKILL 30 s later, and the script exits non-zero
# without copying anything to /output.
set -eu
name=$1
shift
case "$name" in
  ""|*/*|.*) echo "studio-hf-render: bad output name '$name'" >&2; exit 2 ;;
esac
mkdir -p /tmp/studio-out
hyperframes_bin=/opt/hyperframes/node_modules/.bin/hyperframes
deadline=${STUDIO_HF_DEADLINE_SECONDS:-1860}
case "$deadline" in
  ""|*[!0-9]*|0) echo "studio-hf-render: bad STUDIO_HF_DEADLINE_SECONDS '$deadline'" >&2; exit 2 ;;
esac
PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome/chrome-headless-shell-linux64/chrome-headless-shell \
PUPPETEER_EXECUTABLE_PATH=/opt/chrome/chrome-headless-shell-linux64/chrome-headless-shell \
  timeout --signal=TERM --kill-after=30 "$deadline" \
  "$hyperframes_bin" render "$@" --output "/tmp/studio-out/$name"
cp -R "/tmp/studio-out/$name" "/output/.partial-$name"
mv "/output/.partial-$name" "/output/$name"
