# The studio's HyperFrames render image (plan Phase 2, HyperFrames adapter spike).
#
# Derived from HyperFrames' own `packages/cli/src/docker/Dockerfile.render`
# (heygen-com/hyperframes v0.8.78), with every input that upstream leaves
# floating pinned, because a render image is only reproducible if it is:
#
# - base image by digest (upstream: the `node:22-bookworm-slim` tag);
# - Debian packages from a dated snapshot.debian.org archive (upstream: the
#   live mirror at build time);
# - chrome-headless-shell at an exact Chrome for Testing version (upstream:
#   `@stable`, i.e. whatever is current on the build day). 149.0.7790.0 is the
#   Chromium version `@remotion/renderer` 4.0.529 pins (TESTED_VERSION), so the
#   two adapters run the same browser version and cross-engine differences are
#   the engines', not Chrome's;
# - the HyperFrames CLI and every transitive npm dependency through the
#   committed package-lock.json and `npm ci` (upstream: `npm install -g
#   hyperframes@<version>`, which resolves transitive ranges on the build day).
#
# Deliberately NOT installed: upstream's font packages (Noto, Liberation, …).
# The studio renders only the bundled Inter (loaded by the page) under its own
# fontconfig (fontconfig/fonts.conf, mounted and set as FONTCONFIG_FILE at run
# time), which names no font directory: `fc-list` under it lists 0 fonts, even
# though DejaVu arrives here as a dependency of the Chrome libraries.
#
# Telemetry: HYPERFRAMES_NO_TELEMETRY=1 and DO_NOT_TRACK=1 are baked in, and the
# adapter also runs the container with `--network none`.
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

ARG DEBIAN_SNAPSHOT=20260925T000000Z
ARG CHROME_VERSION=149.0.7790.0

RUN printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n' \
      "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends \
      ca-certificates unzip ffmpeg fontconfig \
      libgbm1 libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
      libxdamage1 libxrandr2 libcups2 libasound2 libpangocairo-1.0-0 \
      libxshmfence1 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/* && apt-get clean

ENV HYPERFRAMES_NO_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    CONTAINER=true \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    npm_config_update_notifier=false

WORKDIR /opt/hyperframes
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund \
    && ./node_modules/.bin/browsers install "chrome-headless-shell@${CHROME_VERSION}" --path /opt/chrome \
    && SHELL_PATH=$(find /opt/chrome -name chrome-headless-shell -type f | head -1) \
    && test -n "$SHELL_PATH" \
    && printf '#!/bin/sh\nexport PRODUCER_HEADLESS_SHELL_PATH=%s\nexport PUPPETEER_EXECUTABLE_PATH=%s\nexec /opt/hyperframes/node_modules/.bin/hyperframes render "$@"\n' "$SHELL_PATH" "$SHELL_PATH" > /usr/local/bin/hf-render \
    && chmod +x /usr/local/bin/hf-render \
    && /opt/hyperframes/node_modules/.bin/hyperframes --version

WORKDIR /project
ENTRYPOINT ["hf-render"]
