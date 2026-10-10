// expect: no-restricted-syntax :: Only src/cli may dynamic-import an adapter
export const load = () => import("../adapters/remotion/render");
