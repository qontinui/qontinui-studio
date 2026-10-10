/**
 * Register the bundled fonts with the document before the first frame is
 * captured. Engine-neutral: each adapter supplies how a font file path maps
 * to a URL and how it holds the frame until the promise settles.
 */
import { FONT_FAMILY, fontFiles } from "../api/typography";

let loading: Promise<void> | null = null;

export function loadStudioFonts(urlFor: (file: string) => string): Promise<void> {
  if (loading) return loading;
  loading = Promise.all(
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
  return loading;
}
