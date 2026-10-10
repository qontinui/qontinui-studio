/**
 * `pnpm hyperframes:image`: build the HyperFrames render image from
 * docker/hyperframes/, labelled with the SHA-256 of its input files so the
 * adapter can tell a current image from a stale one.
 */
import { spawnSync } from "node:child_process";

import { DEFAULT_HYPERFRAMES_IMAGE, IMAGE_CONTEXT, imageInputsSha256 } from "../src/adapters/hyperframes/image";

const inputs = imageInputsSha256();
const result = spawnSync(
  "docker",
  [
    "build",
    "--platform",
    "linux/amd64",
    "--build-arg",
    `STUDIO_INPUTS_SHA256=${inputs}`,
    "-t",
    DEFAULT_HYPERFRAMES_IMAGE,
    IMAGE_CONTEXT,
  ],
  { stdio: "inherit" },
);
if (result.status !== 0) process.exit(result.status ?? 1);
const id = spawnSync("docker", ["image", "inspect", "--format", "{{.Id}}", DEFAULT_HYPERFRAMES_IMAGE], { encoding: "utf8" });
process.stdout.write(`${DEFAULT_HYPERFRAMES_IMAGE}\n  id ${id.stdout.trim()}\n  inputs-sha256 ${inputs}\n`);
