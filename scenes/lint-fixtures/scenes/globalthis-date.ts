// expect: no-restricted-globals
export const t = () => globalThis.Date.now();
