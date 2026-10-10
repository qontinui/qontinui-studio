/**
 * Identity of the HyperFrames render image (docker/hyperframes/).
 *
 * The image carries a LABEL with the SHA-256 of the files it was built from
 * (Dockerfile, package.json, package-lock.json). The adapter recomputes that
 * hash from the checked-in files and refuses an image whose label differs, so
 * a stale or foreign image cannot render silently. It then runs the image by
 * its resolved `sha256:` id, never by a tag that could move in between.
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
export const IMAGE_CONTEXT = path.join(PACKAGE_ROOT, "docker/hyperframes");
export const IMAGE_INPUTS = ["Dockerfile", "package.json", "package-lock.json", "studio-hf-render.sh"] as const;
export const INPUTS_LABEL = "org.qontinui.studio.inputs-sha256";
export const DEFAULT_HYPERFRAMES_IMAGE = "studio-hyperframes:0.8.78";

/** SHA-256 over the image's input files, each prefixed by its name and length. */
export function imageInputsSha256(contextDir: string = IMAGE_CONTEXT): string {
  const hash = createHash("sha256");
  for (const name of IMAGE_INPUTS) {
    const bytes = readFileSync(path.join(contextDir, name));
    hash.update(`${name}\0${bytes.length}\0`);
    hash.update(bytes);
  }
  return hash.digest("hex");
}

/**
 * A docker image reference: `sha256:<64 hex>`, or
 * `[registry[:port]/]name[/name…][:tag][@sha256:<64 hex>]`. Anything else is
 * refused, in particular a value starting with `-` that docker would parse as
 * an option.
 */
const COMPONENT = "[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*";
const IMAGE_REF = new RegExp(
  `^(?:sha256:[a-f0-9]{64}|(?:[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]+)?/)?${COMPONENT}(?:/${COMPONENT})*` +
    `(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?)$`,
);

export function imageRefProblem(ref: string): string | null {
  if (ref.startsWith("-")) return `image reference "${ref}" starts with "-" (it would be read as a docker option)`;
  if (!IMAGE_REF.test(ref)) return `"${ref}" is not a docker image reference`;
  return null;
}
