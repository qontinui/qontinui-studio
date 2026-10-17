# style/

The studio style guide (plan
`2026-10-09-qontinui-marketing-studio-animated-process-explainers`, D4).

- `guide.yaml` is the guide. It covers the twin metaphor, the icon set, the
  looks and the reference set. It names palette, motion and typography tokens
  but gives no values for them. Their values come from
  `@qontinui/design-tokens` (1.3.0 or later), the one place they are defined.
- `icons/*.svg` is the icon set. Each icon is line work on a 24×24 grid in
  `currentColor`. The renderer refuses any element or attribute outside the
  icon contract in `qontinui_studio.stylesheet`.
- The `*.md` files are prompt fragments, called looks. A brief names one as its
  `style_guide`, and the brief's prompt includes the fragment word for word.
  - `studio-style-v1.md` is the Qontinui brand look.
  - `warm-minimal.md` is the example look for the small-business template
    sites. It is not the Qontinui brand. It stays separate because appending
    the brand guide to it would change every prompt those sites send.

Render the style-sheet page:

```bash
poetry run studio style render --tokens-css <qontinui-design-tokens>/src/tokens.css
# -> .studio/style/index.html
```

The page fills in each token's value from that `tokens.css`. If the guide names
a token the file does not declare, the render is refused. Without
`--tokens-css`, or `STUDIO_DESIGN_TOKENS_CSS`, every value shows as unresolved.
The page also checks that each colour in a look is one of the token values. The
reference set shown on the page is a library query, `studio library list --tag
studio-style-v1 --status approved`, run against the `--root` project.
