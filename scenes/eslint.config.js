// @ts-check
import js from "@eslint/js";
import tseslint from "typescript-eslint";

/**
 * Engines and wall-clock animation libraries. Scenes and primitives import
 * only the studio API (`src/api`) and other primitives (plan D7); the engine
 * lives behind `src/adapters/**`.
 */
const ENGINE_AND_CLOCK_IMPORTS = [
  {
    group: ["remotion", "remotion/*", "@remotion/*"],
    message: "Scenes and primitives import only the studio API (src/api), never the engine (plan D7).",
  },
  {
    group: ["@revideo/*", "@motion-canvas/*", "hyperframes", "@hyperframes/*", "@heygen/*"],
    message: "Scenes and primitives import only the studio API (src/api), never an engine (plan D7).",
  },
  {
    group: ["framer-motion", "framer-motion/*", "motion", "motion/*", "react-spring", "@react-spring/*", "gsap", "gsap/*", "animejs", "popmotion"],
    message: "Wall-clock animation breaks frame identity; derive motion from the frame with src/api/motion.",
  },
  {
    group: ["**/adapters", "**/adapters/**", "**/cli", "**/cli/**"],
    message: "Scenes and primitives may not reach into an adapter or the CLI (plan D7).",
  },
];

/** React state/effect hooks would let a frame depend on render history. */
const IMPURE_REACT_HOOKS = {
  name: "react",
  importNames: ["useEffect", "useLayoutEffect", "useInsertionEffect", "useState", "useReducer", "useRef", "useTransition", "useDeferredValue", "useSyncExternalStore"],
  message: "A scene is a pure function of (trace, frame): no state, refs or effects.",
};

export default tseslint.config(
  { ignores: ["node_modules/**", "out/**", ".golden-out/**", "bin/**"] },
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
    },
  },
  {
    // Scene purity (plan Phase 2, scout addendum): a scene is a pure function
    // of (trace, frame). No engine, no clock, no randomness, no CSS-timed
    // animation.
    files: ["src/scenes/**/*.{ts,tsx}", "src/primitives/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": ["error", { patterns: ENGINE_AND_CLOCK_IMPORTS, paths: [IMPURE_REACT_HOOKS] }],
      "no-restricted-globals": [
        "error",
        { name: "Date", message: "No clock in scenes; use eventTimeMs/format helpers from src/api." },
        { name: "performance", message: "No clock in scenes." },
        { name: "requestAnimationFrame", message: "No wall-clock animation in scenes; derive from the frame." },
        { name: "cancelAnimationFrame", message: "No wall-clock animation in scenes." },
        { name: "setTimeout", message: "No timers in scenes." },
        { name: "setInterval", message: "No timers in scenes." },
        { name: "fetch", message: "Scenes render from the trace only." },
        { name: "crypto", message: "No randomness in scenes." },
      ],
      "no-restricted-properties": [
        "error",
        { object: "Math", property: "random", message: "No randomness in scenes; same trace must give same frames." },
        { object: "Date", property: "now", message: "No clock in scenes." },
        { object: "window", property: "requestAnimationFrame", message: "No wall-clock animation in scenes." },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: "Property[key.name=/^(transition|transitionDuration|transitionProperty|transitionDelay|animation|animationName|animationDuration|animationDelay)$/]",
          message: "CSS transitions/animations run on the wall clock; interpolate from the frame instead.",
        },
        {
          selector: "JSXOpeningElement[name.name=/^(animate|animateTransform|animateMotion|set)$/]",
          message: "SVG SMIL animation runs on the wall clock; interpolate from the frame instead.",
        },
      ],
    },
  },
);
