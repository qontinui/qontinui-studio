// @ts-check
import js from "@eslint/js";
import tseslint from "typescript-eslint";

/**
 * Engine module specifiers. Only `src/adapters/**` may load an engine (plan
 * D7). Kept as one regex source so the static-import ban, the dynamic
 * `import()` ban and the `require()` ban cannot drift apart.
 */
// esquery regexes cannot contain "/", so "." stands in for the scope separator.
const ENGINE_SOURCE =
  "^(remotion|@remotion.|@revideo.|@motion-canvas.|hyperframes|@hyperframes.|@heygen.)";

const ENGINE_IMPORTS = [
  {
    group: ["remotion", "remotion/*", "@remotion/*"],
    message: "Only src/adapters/** may import the engine; everything else uses the studio API (plan D7).",
  },
  {
    group: ["@revideo/*", "@motion-canvas/*", "hyperframes", "hyperframes/*", "@hyperframes/*", "@heygen/*"],
    message: "Only src/adapters/** may import an engine (plan D7).",
  },
];

/** Static imports of an adapter: the CLI reaches it only via dynamic import(). */
const ADAPTER_IMPORTS = [
  {
    group: ["**/adapters", "**/adapters/**"],
    message: "Only a dynamic import() may load an adapter (src/cli); scenes, primitives and the API never do (plan D7).",
  },
];

const CLOCK_IMPORTS = [
  {
    group: ["framer-motion", "framer-motion/*", "motion", "motion/*", "react-spring", "@react-spring/*", "gsap", "gsap/*", "animejs", "popmotion"],
    message: "Wall-clock animation breaks frame identity; derive motion from the frame with src/api/motion.",
  },
  {
    group: ["**/cli", "**/cli/**"],
    message: "Scenes and primitives may not reach into the CLI.",
  },
];

const IMPURE_HOOKS = [
  "useEffect",
  "useLayoutEffect",
  "useInsertionEffect",
  "useState",
  "useReducer",
  "useRef",
  "useTransition",
  "useDeferredValue",
  "useSyncExternalStore",
  "useId",
];

/** Bans shared by every non-adapter source file: no engine, however it is loaded. */
const engineSyntaxBans = [
  {
    selector: `ImportExpression[source.value=/${ENGINE_SOURCE}/]`,
    message: "Dynamic import of an engine outside src/adapters (plan D7).",
  },
  {
    selector: `CallExpression[callee.name='require'][arguments.0.value=/${ENGINE_SOURCE}/]`,
    message: "require() of an engine outside src/adapters (plan D7).",
  },
  {
    selector: "ImportExpression:not([source.type='Literal'])",
    message: "Dynamic import() needs a literal specifier so the engine ban can see it.",
  },
];

const CSS_TIMED = "(transition|animation)";
/** Scene purity (plan Phase 2, scout addendum): no clock, randomness, timers or CSS-timed motion. */
const puritySyntaxBans = [
  // Wall-clock CSS, however the key is spelled.
  {
    selector: `Property > Identifier.key[name=/^(Webkit|Moz|ms|O)?${CSS_TIMED}/i]`,
    message: "CSS transitions/animations run on the wall clock; interpolate from the frame instead.",
  },
  {
    selector: `Property > Literal.key[value=/^(-(webkit|moz|ms|o)-)?${CSS_TIMED}/i]`,
    message: "CSS transitions/animations run on the wall clock; interpolate from the frame instead.",
  },
  {
    selector: "Property[computed=true]",
    message: "Computed property keys are not allowed in scenes (they can smuggle CSS transition/animation keys).",
  },
  {
    selector: `Literal[value=/@keyframes|${CSS_TIMED}\\s*:/i]`,
    message: "CSS keyframes/transitions in strings run on the wall clock.",
  },
  {
    selector: `TemplateElement[value.raw=/@keyframes|${CSS_TIMED}\\s*:/i]`,
    message: "CSS keyframes/transitions in strings run on the wall clock.",
  },
  {
    selector: "JSXOpeningElement[name.name=/^(style|animate|animateTransform|animateMotion|set)$/]",
    message: "<style> and SVG SMIL elements animate on the wall clock; interpolate from the frame instead.",
  },
  {
    selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
    message: "No raw HTML in scenes: it can carry styles and scripts the purity rules cannot see.",
  },
  // Math is allowed only as `Math.<name>` with a static name (no aliasing,
  // no computed access) so Math.random cannot be reached indirectly.
  {
    selector: "Identifier[name='Math']:not(MemberExpression[computed=false] > Identifier.object)",
    message: "Use Math only as Math.<fn>; aliasing it hides Math.random from the purity rules.",
  },
  // React: no state, refs or effects, also not via a default/namespace import.
  {
    selector: "ImportDeclaration[source.value='react'] > ImportDefaultSpecifier",
    message: "Import React hooks by name so the purity rules can see them.",
  },
  {
    selector: "ImportDeclaration[source.value='react'] > ImportNamespaceSpecifier",
    message: "Import React hooks by name so the purity rules can see them.",
  },
  {
    selector: `MemberExpression[property.name=/^(${IMPURE_HOOKS.join("|")})$/]`,
    message: "A scene is a pure function of (trace, frame): no state, refs or effects.",
  },
  // Module-level mutable state survives between frames.
  {
    selector: "Program > VariableDeclaration[kind=/^(let|var)$/]",
    message: "No module-level mutable state in scenes: it can carry values between frames.",
  },
  {
    selector: "Program > ExportNamedDeclaration > VariableDeclaration[kind=/^(let|var)$/]",
    message: "No module-level mutable state in scenes: it can carry values between frames.",
  },
];

export default tseslint.config(
  { ignores: ["node_modules/**", "out/**", ".golden-out/**", "bin/**", "lint-fixtures/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        console: "readonly",
        process: "readonly",
        document: "readonly",
        FontFace: "readonly",
      },
    },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "@typescript-eslint/no-require-imports": "error",
    },
  },
  {
    // The engine seam (plan D7): nothing under src/ but the adapters loads an
    // engine — not by import, export-from, import() or require().
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/adapters/**"],
    rules: {
      "no-restricted-imports": ["error", { patterns: [...ENGINE_IMPORTS, ...ADAPTER_IMPORTS] }],
      "no-restricted-syntax": ["error", ...engineSyntaxBans],
    },
  },
  {
    // Scene purity: a scene is a pure function of (trace, frame). These
    // rules REPLACE the ones above for these files, so they restate the
    // engine bans.
    files: ["src/scenes/**/*.{ts,tsx}", "src/primitives/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [...ENGINE_IMPORTS, ...ADAPTER_IMPORTS, ...CLOCK_IMPORTS],
          paths: [
            {
              name: "react",
              importNames: IMPURE_HOOKS,
              message: "A scene is a pure function of (trace, frame): no state, refs or effects.",
            },
          ],
        },
      ],
      "no-restricted-globals": [
        "error",
        ...["globalThis", "window", "self", "global", "document", "navigator"].map((name) => ({
          name,
          message: "No ambient globals in scenes: they reach the clock, randomness and the DOM.",
        })),
        { name: "Date", message: "No clock in scenes; use eventTimeMs/format helpers from src/api." },
        { name: "performance", message: "No clock in scenes." },
        { name: "Intl", message: "No locale/timezone-dependent formatting in scenes; use src/api/format." },
        { name: "process", message: "No process state (hrtime, env) in scenes." },
        { name: "requestAnimationFrame", message: "No wall-clock animation in scenes; derive from the frame." },
        { name: "cancelAnimationFrame", message: "No wall-clock animation in scenes." },
        { name: "setTimeout", message: "No timers in scenes." },
        { name: "setInterval", message: "No timers in scenes." },
        { name: "queueMicrotask", message: "No scheduled work in scenes." },
        { name: "fetch", message: "Scenes render from the trace only." },
        { name: "crypto", message: "No randomness in scenes." },
      ],
      "no-restricted-properties": [
        "error",
        { object: "Math", property: "random", message: "No randomness in scenes; same trace must give same frames." },
        { object: "Date", property: "now", message: "No clock in scenes." },
        { object: "performance", property: "now", message: "No clock in scenes." },
        { object: "process", property: "hrtime", message: "No clock in scenes." },
        { object: "crypto", property: "getRandomValues", message: "No randomness in scenes." },
        { object: "crypto", property: "randomUUID", message: "No randomness in scenes." },
      ],
      "no-restricted-syntax": ["error", ...engineSyntaxBans, ...puritySyntaxBans],
    },
  },
);
