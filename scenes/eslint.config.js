// @ts-check
import js from "@eslint/js";
import tseslint from "typescript-eslint";

/*
 * Lint rules for the studio's boundaries. ESLint replaces a rule's options
 * per matching config block (the last block wins), so every block below is
 * built by `restrictions()` from the full set of bans that apply to its
 * files; no block can silently drop another's ban.
 *
 * Lint sees one file at a time. The resolved-graph test
 * (tests/unit/engine-graph.test.ts) is the authority for the engine seam.
 */

// esquery regexes cannot contain "/", so "." stands in for the scope separator.
const ENGINE_SOURCE =
  "^(remotion|@remotion.|@revideo.|@motion-canvas.|hyperframes|@hyperframes.|@heygen.)";

// ---------------------------------------------------------------------------
// no-restricted-imports building blocks
// ---------------------------------------------------------------------------

// Anchored regexes: a gitignore-style group such as "remotion/*" would also
// match a relative path like "../adapters/remotion/render".
const ENGINE_IMPORTS = [
  {
    regex: "^(remotion|@remotion/.+)$",
    message: "Only src/adapters/** may import the engine; everything else uses the studio API (plan D7).",
  },
  {
    regex: "^(@revideo/.+|@motion-canvas/.+|hyperframes(/.+)?|@hyperframes/.+|@heygen/.+)$",
    message: "Only src/adapters/** may import an engine (plan D7).",
  },
];

const ADAPTER_IMPORTS = [
  {
    group: ["**/adapters", "**/adapters/**"],
    message: "Only src/cli may load an adapter, and only by dynamic import() (plan D7).",
  },
];

/** createRequire would load anything, invisibly to the import rules. */
const MODULE_PATHS = [
  { name: "module", message: "createRequire/module loading outside src/adapters bypasses the engine seam (plan D7)." },
  { name: "node:module", message: "createRequire/module loading outside src/adapters bypasses the engine seam (plan D7)." },
];

const TESTING_IMPORTS = [
  {
    group: ["**/trace/testing", "**/trace/testing.ts"],
    message: "trace/testing (attestReplayForTests) may be imported only under tests/** (plan D2).",
  },
];

const CLOCK_IMPORTS = [
  {
    group: ["framer-motion", "framer-motion/*", "motion", "motion/*", "react-spring", "@react-spring/*", "gsap", "gsap/*", "animejs", "popmotion"],
    message: "Wall-clock animation breaks frame identity; derive motion from the frame with src/api/motion.",
  },
  {
    group: ["react-dom", "react-dom/*"],
    message: "No react-dom in scenes (createPortal escapes the scene and its mode label).",
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

// ---------------------------------------------------------------------------
// no-restricted-syntax building blocks
// ---------------------------------------------------------------------------

const ENGINE_SYNTAX = [
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
  {
    selector: "Identifier[name='createRequire']",
    message: "createRequire outside src/adapters bypasses the engine seam (plan D7).",
  },
  {
    selector: "MemberExpression[property.name='getBuiltinModule']",
    message: "process.getBuiltinModule outside src/adapters bypasses the engine seam (plan D7).",
  },
  // Computed access on process (process["getBuiltin" + "Module"]), also
  // through globalThis.process and through TypeScript casts.
  ...[
    "MemberExpression[computed=true][object.name='process']",
    "MemberExpression[computed=true][object.property.name='process']",
    "MemberExpression[computed=true][object.expression.name='process']",
    "MemberExpression[computed=true][object.expression.property.name='process']",
    "MemberExpression[computed=true][object.expression.expression.name='process']",
    "MemberExpression[computed=true][object.expression.expression.property.name='process']",
  ].map((selector) => ({
    selector,
    message: "Computed access on process outside src/adapters can reach getBuiltinModule (plan D7).",
  })),
];

const ADAPTER_DYNAMIC_SYNTAX = [
  {
    selector: "ImportExpression[source.value=/adapters/]",
    message: "Only src/cli may dynamic-import an adapter (plan D7).",
  },
];

const PROVENANCE_ONLY =
  "recordReplayProvenance may be used only by fromBuildRecord (and the test-only hook): it is what makes a replay renderable (plan D2).";
const PROVENANCE_SYNTAX = [
  { selector: "Identifier[name='recordReplayProvenance']", message: PROVENANCE_ONLY },
  { selector: "Literal[value='recordReplayProvenance']", message: PROVENANCE_ONLY },
  // No wholesale or namespace access to the provenance module, which would
  // reach recordReplayProvenance without naming it.
  { selector: "ExportAllDeclaration[source.value=/provenance/]", message: PROVENANCE_ONLY },
  { selector: "ImportDeclaration[source.value=/provenance/] > ImportNamespaceSpecifier", message: PROVENANCE_ONLY },
  { selector: "ImportExpression[source.value=/provenance/]", message: PROVENANCE_ONLY },
];

const CSS_TIMED = "(transition|animation)";
/** Scene purity (plan Phase 2, scout addendum): no clock, randomness, timers or wall-clock motion. */
const PURITY_SYNTAX = [
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
  // Media that plays on its own clock.
  {
    selector: "JSXOpeningElement[name.name=/^(video|audio|iframe|marquee)$/]",
    message: "Media elements play on the wall clock; a scene draws every frame from (trace, frame).",
  },
  {
    selector: "JSXAttribute[name.name=/^(autoPlay|autoplay)$/]",
    message: "autoPlay plays on the wall clock.",
  },
  {
    selector: "JSXOpeningElement[name.name='img'] > JSXAttribute[name.name='src'] Literal[value=/\\.(gif|apng)(\\?.*)?$/i]",
    message: "Animated images play on the wall clock; use a still image.",
  },
  {
    selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
    message: "No raw HTML in scenes: it can carry styles and scripts the purity rules cannot see.",
  },
  // Blur. Chrome caches blurred masks across paints, so a blurred glow came
  // out differently depending on which frames were rendered before it (see
  // src/api/glow.ts). Glows use solidRings(); nothing in a scene blurs.
  {
    selector: "Property > Identifier.key[name=/^(Webkit|Moz|ms)?([fF]ilter|[bB]ackdropFilter|[tT]extShadow)$/]",
    message: "No blur or filter in scenes: blurred paint depends on render history. Use solidRings() from src/api/glow.",
  },
  {
    selector: "Property > Literal.key[value=/^(-(webkit|moz|ms|o)-)?(filter|backdrop-filter|text-shadow|box-shadow)$/i]",
    message: "No blur or filter in scenes: blurred paint depends on render history. Use solidRings() from src/api/glow.",
  },
  {
    selector:
      "Property[key.name=/^(Webkit|Moz)?[bB]oxShadow$/]:not([value.type='CallExpression'][value.callee.name='solidRings'])",
    message: "boxShadow in scenes must be solidRings(...) (zero blur): blurred paint depends on render history.",
  },
  // The boxShadow rule trusts the NAME solidRings, so the name must be the
  // studio's: imported from src/api (glow or index), never bound locally.
  ...[
    "VariableDeclarator > Identifier.id[name='solidRings']",
    "FunctionDeclaration > Identifier.id[name='solidRings']",
    ":function > Identifier.params[name='solidRings']",
    "ObjectPattern > Property > Identifier.value[name='solidRings']",
    "ImportDefaultSpecifier[local.name='solidRings']",
    "ImportNamespaceSpecifier[local.name='solidRings']",
    "ImportSpecifier[local.name='solidRings'][imported.name!='solidRings']",
    "ImportDeclaration:not([source.value=/[.][.].api(.glow)?$/]) > ImportSpecifier[local.name='solidRings']",
  ].map((selector) => ({
    selector,
    message: "solidRings must be the one imported from src/api/glow (or src/api); a local solidRings could blur.",
  })),
  {
    selector: "Literal[value=/blur\\(|drop-shadow\\(/i]",
    message: "No blur()/drop-shadow() in scenes: blurred paint depends on render history.",
  },
  {
    selector: "TemplateElement[value.raw=/blur\\(|drop-shadow\\(/i]",
    message: "No blur()/drop-shadow() in scenes: blurred paint depends on render history.",
  },
  {
    selector: "JSXOpeningElement[name.name=/^(filter|feGaussianBlur|feDropShadow|feMorphology|feConvolveMatrix)$/]",
    message: "No SVG filters in scenes: blurred paint depends on render history.",
  },
  {
    selector: "JSXAttribute[name.name=/^(filter|backdropFilter)$/]",
    message: "No SVG/CSS filter attribute in scenes: blurred paint depends on render history.",
  },
  // No reaching Function through prototypes (x.constructor.constructor).
  {
    selector: "MemberExpression[property.name='constructor']",
    message: "No .constructor access in scenes (x.constructor.constructor reaches Function, i.e. eval).",
  },
  {
    selector: "MemberExpression[property.value='constructor']",
    message: "No .constructor access in scenes (x.constructor.constructor reaches Function, i.e. eval).",
  },
  // Math is allowed only as `Math.<name>` with a static name.
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
  {
    selector:
      "Program > VariableDeclaration > VariableDeclarator > NewExpression[callee.name=/^(Map|Set|WeakMap|WeakSet|WeakRef|Array)$/]",
    message: "No module-level mutable containers in scenes: they can carry values between frames.",
  },
  {
    selector:
      "Program > ExportNamedDeclaration > VariableDeclaration > VariableDeclarator > NewExpression[callee.name=/^(Map|Set|WeakMap|WeakSet|WeakRef|Array)$/]",
    message: "No module-level mutable containers in scenes: they can carry values between frames.",
  },
];

const PURITY_GLOBALS = [
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
];

const PURITY_PROPERTIES = [
  { object: "Math", property: "random", message: "No randomness in scenes; same trace must give same frames." },
  { object: "Date", property: "now", message: "No clock in scenes." },
  { object: "performance", property: "now", message: "No clock in scenes." },
  { object: "process", property: "hrtime", message: "No clock in scenes." },
  { object: "crypto", property: "getRandomValues", message: "No randomness in scenes." },
  { object: "crypto", property: "randomUUID", message: "No randomness in scenes." },
];

/**
 * The rules for one block, from the bans that apply to its files.
 * @param {{engine?: boolean, adapterStatic?: boolean, adapterDynamic?: boolean,
 *          provenance?: boolean, testing?: boolean, purity?: boolean}} bans
 */
function restrictions(bans) {
  const patterns = [
    ...(bans.engine ? ENGINE_IMPORTS : []),
    ...(bans.adapterStatic ? ADAPTER_IMPORTS : []),
    ...(bans.testing ? TESTING_IMPORTS : []),
    ...(bans.purity ? CLOCK_IMPORTS : []),
  ];
  const paths = [
    ...(bans.engine ? MODULE_PATHS : []),
    ...(bans.purity
      ? [{ name: "react", importNames: IMPURE_HOOKS, message: "A scene is a pure function of (trace, frame): no state, refs or effects." }]
      : []),
  ];
  const syntax = [
    ...(bans.engine ? ENGINE_SYNTAX : []),
    ...(bans.adapterDynamic ? ADAPTER_DYNAMIC_SYNTAX : []),
    ...(bans.provenance ? PROVENANCE_SYNTAX : []),
    ...(bans.purity ? PURITY_SYNTAX : []),
  ];
  /** @type {Record<string, unknown>} */
  const rules = {
    "no-restricted-imports": patterns.length || paths.length ? ["error", { patterns, paths }] : "off",
    "no-restricted-syntax": syntax.length ? ["error", ...syntax] : "off",
  };
  if (bans.purity) {
    rules["no-restricted-globals"] = ["error", ...PURITY_GLOBALS];
    rules["no-restricted-properties"] = ["error", ...PURITY_PROPERTIES];
  }
  return rules;
}

/** Files allowed to call recordReplayProvenance: its definition, fromBuildRecord, and the test-only hook. */
const PROVENANCE_WRITERS = ["src/api/trace/provenance.ts", "src/api/trace/from-build-record.ts", "src/api/trace/testing.ts"];

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
        window: "readonly",
        globalThis: "readonly",
        self: "readonly",
        setTimeout: "readonly",
        setInterval: "readonly",
        FontFace: "readonly",
      },
    },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "@typescript-eslint/no-require-imports": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      // Everywhere (scripts, config, tests): only fromBuildRecord makes a
      // replay renderable, and only tests/** may reach the test-only hook.
      ...restrictions({ provenance: true, testing: true }),
    },
  },
  {
    files: ["tests/**/*.{ts,tsx}"],
    rules: restrictions({ provenance: true }),
  },
  {
    // The engine seam (plan D7) for everything under src/ but the adapters
    // and the CLI: no engine by any route, no adapter by any route.
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/adapters/**", "src/cli/**"],
    rules: restrictions({ engine: true, adapterStatic: true, adapterDynamic: true, provenance: true, testing: true }),
  },
  {
    // The CLI: no engine; an adapter only by dynamic import().
    files: ["src/cli/**/*.{ts,tsx}"],
    rules: restrictions({ engine: true, adapterStatic: true, provenance: true, testing: true }),
  },
  {
    files: PROVENANCE_WRITERS,
    rules: restrictions({ engine: true, adapterStatic: true, adapterDynamic: true }),
  },
  {
    // Scene purity: a scene is a pure function of (trace, frame).
    files: ["src/scenes/**/*.{ts,tsx}", "src/primitives/**/*.{ts,tsx}", "src/compositions/**/*.{ts,tsx}", "src/api/program-root.tsx"],
    rules: restrictions({
      engine: true,
      adapterStatic: true,
      adapterDynamic: true,
      provenance: true,
      testing: true,
      purity: true,
    }),
  },
);
