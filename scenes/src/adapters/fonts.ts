/**
 * Register the bundled fonts with the document before the first frame is
 * captured. Engine-neutral: each adapter supplies how a font file path maps
 * to a URL and how it holds the frame until the promise settles.
 */
import { FONT_FAMILY, fontFiles } from "../api/typography";

/** One load per font source (a page may embed players with different asset bases). */
const loading = new Map<string, Promise<void>>();

/**
 * @param sourceKey identifies where the files come from (e.g. the asset base
 *   URL); loads are cached per key, and a failed load is not cached.
 */
export function loadStudioFonts(sourceKey: string, urlFor: (file: string) => string): Promise<void> {
  const cached = loading.get(sourceKey);
  if (cached) return cached;
  const promise = Promise.all(
    fontFiles.map(async ({ weight, file }) => {
      const face = new FontFace(FONT_FAMILY, `url("${urlFor(file)}") format("woff2")`, {
        weight: String(weight),
        style: "normal",
        display: "block",
      });
      await face.load();
      document.fonts.add(face);
    }),
  ).then(() => undefined);
  loading.set(sourceKey, promise);
  promise.catch(() => loading.delete(sourceKey));
  return promise;
}
