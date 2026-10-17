/** Content hashes and content-addressed media names, shared by `compose` and the publish gate. */
import { createHash } from "node:crypto";
import path from "node:path";

export function sha256(bytes: Uint8Array | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

/** A media name: content-addressed, so equal files share one name and nothing collides. */
export function mediaName(bytes: Uint8Array, file: string): string {
  const ext = path.extname(file).toLowerCase().replace(/[^.a-z0-9]/g, "");
  return `media/${sha256(bytes).slice(0, 24)}${ext}`;
}
