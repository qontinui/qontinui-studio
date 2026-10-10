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
set -eu
name=$1
shift
case "$name" in
  ""|*/*|.*) echo "studio-hf-render: bad output name '$name'" >&2; exit 2 ;;
esac
mkdir -p /tmp/studio-out
hyperframes_bin=/opt/hyperframes/node_modules/.bin/hyperframes
PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome/chrome-headless-shell-linux64/chrome-headless-shell \
PUPPETEER_EXECUTABLE_PATH=/opt/chrome/chrome-headless-shell-linux64/chrome-headless-shell \
  "$hyperframes_bin" render "$@" --output "/tmp/studio-out/$name"
cp -R "/tmp/studio-out/$name" "/output/.partial-$name"
mv "/output/.partial-$name" "/output/$name"
