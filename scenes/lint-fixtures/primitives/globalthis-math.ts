// expect: no-restricted-globals
export const r = () => globalThis.Math.random();
