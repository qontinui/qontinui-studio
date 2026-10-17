# Vendored: explainroo (MIT)

Source: <https://github.com/vincentsch/explainroo>, commit
`025809e3e36b9e1cd548f19695c2869169c62d83`. Copyright (c) 2026 Vincent
Schmalbach. Licence: MIT, reproduced in full in `LICENSE` beside this file;
it travels with these files wherever they are copied.

| File | Upstream | What it is |
| --- | --- | --- |
| `align.js` | `src/align.js` | number- and name-aware word alignment (script words vs heard words) |
| `script.js` | `src/script.js` | the narration cue grammar (`[#mark]`, `[pause s]`, `{shown\|spoken}`), the script parser, speech chunking, mark resolution |
| `*.d.ts` | (ours) | type declarations for the two files |

Each file's header lists exactly what was changed: the Mandarin and Japanese
paths were removed (they pull in opencc-js, pinyin-pro and the misaki
readings, and the studio's bundled fonts draw no CJK). Nothing else was
changed, so the alignment behaves as upstream for English and other
Latin-script text. These files are not linted (they are upstream's code);
`tests/unit/narration.test.ts` ports upstream's English alignment tests.
