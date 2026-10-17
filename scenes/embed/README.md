# @qontinui/studio-scenes (embeddable player)

The web player for the Qontinui studio's animated process explainers, built
from [qontinui-studio](https://github.com/qontinui/qontinui-studio) `scenes/` by
`pnpm embed:pack`. It is not published to npm. A site vendors the tarball and
installs it as a file dependency, recording the studio commit and the tarball's
SHA-256 beside it.

```tsx
import { StudioPlayer, illustrativeTraces } from "@qontinui/studio-scenes/player";

<StudioPlayer
  scene="s1-plan-lifecycle"
  trace={illustrativeTraces["s1-plan-lifecycle"]}
  aspect="16:9"
  assetBaseUrl="/studio" // the PARENT of the URL that serves this package's fonts/
  autoPlay
  loop
/>;
```

- **Peer dependencies:** `react`, `react-dom`, `remotion` and `@remotion/player`
  (exact version). Remotion is source-available under its own licence
  (https://www.remotion.dev/license), not open source. An AGPL site embeds it
  only under an AGPL section 7 additional permission.
- **Lazy loading:** `dist/player.js` is one ESM module. Import it dynamically,
  so the player and Remotion load as their own chunk.
- **Poster first:** `posters/<scene>-16x9.png` is each scene's poster frame.
  It is the frame the player opens on when it is not autoplaying: under
  reduced motion, during server rendering, or without `autoPlay`.
  `manifest.json` lists the posters. Show the poster until the player has
  loaded.
- **Reduced motion:** under `prefers-reduced-motion: reduce` the player never
  autoplays. It shows the poster frame with a play control.
- **Mode label:** every frame carries its trace mode on screen. The traces in
  this package are all ILLUSTRATIVE (hand-authored), and the label says so.

MIT (see LICENSE). The fonts are Inter, under the SIL Open Font License 1.1
(`fonts/OFL.txt`). Third-party code bundled into `dist/player.js` (zod, MIT)
is listed with its licence in `THIRD_PARTY_LICENSES`.
