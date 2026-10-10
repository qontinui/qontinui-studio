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
#   `@stable`, i.e. whatever is current on the build day), downloaded from the
#   official Chrome for Testing URL and checked against a committed SHA-256
#   (computed 2026-10-11 from that URL; its MD5 matched the bucket's own
#   x-goog-hash) before it is unpacked. 149.0.7790.0 is the
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
#
# Identity: `pnpm hyperframes:image` (scripts/hyperframes-image.ts) passes
# STUDIO_INPUTS_SHA256, a hash of this Dockerfile, package.json and
# package-lock.json, and it lands in a LABEL. The adapter refuses an image whose
# label does not match the checked-in files, then runs it by its sha256: id.
#
# Runtime user: `studio` (uid 10001). The adapter overrides it with the
# caller's uid:gid so the frames it writes are the caller's, and runs the
# container read-only with a tmpfs /tmp, all capabilities dropped and
# no-new-privileges.
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

ARG DEBIAN_SNAPSHOT=20260925T000000Z
ARG CHROME_VERSION=149.0.7790.0
ARG CHROME_SHA256=a3b011ab4c726e215cdeb623907a09cfb48f07054a7271fdda555ee2ae4f804d
ARG STUDIO_INPUTS_SHA256=unset

RUN printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n' \
      "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip ffmpeg fontconfig \
      libgbm1 libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
      libxdamage1 libxrandr2 libcups2 libasound2 libpangocairo-1.0-0 \
      libxshmfence1 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/* && apt-get clean

ENV HYPERFRAMES_NO_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    CONTAINER=true \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    npm_config_update_notifier=false

RUN curl -fsSL --retry 5 -o /tmp/chs.zip \
      "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-headless-shell-linux64.zip" \
    && echo "${CHROME_SHA256}  /tmp/chs.zip" | sha256sum -c - \
    && mkdir -p /opt/chrome && unzip -q /tmp/chs.zip -d /opt/chrome && rm /tmp/chs.zip \
    && test -x /opt/chrome/chrome-headless-shell-linux64/chrome-headless-shell

WORKDIR /opt/hyperframes
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund \
    && printf '#!/bin/sh\nexport PRODUCER_HEADLESS_SHELL_PATH=%s\nexport PUPPETEER_EXECUTABLE_PATH=%s\nexec /opt/hyperframes/node_modules/.bin/hyperframes render "$@"\n' \
      /opt/chrome/chrome-headless-shell-linux64/chrome-headless-shell /opt/chrome/chrome-headless-shell-linux64/chrome-headless-shell > /usr/local/bin/hf-render \
    && chmod +x /usr/local/bin/hf-render \
    && /opt/hyperframes/node_modules/.bin/hyperframes --version \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin studio

LABEL org.qontinui.studio.inputs-sha256="${STUDIO_INPUTS_SHA256}" \
      org.qontinui.studio.hyperframes-version="0.8.78" \
      org.qontinui.studio.chrome-version="${CHROME_VERSION}"

USER studio
WORKDIR /project
ENTRYPOINT ["hf-render"]
