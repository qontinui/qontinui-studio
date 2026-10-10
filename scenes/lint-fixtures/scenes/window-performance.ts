// expect: no-restricted-globals
export const t = () => window.performance.now();
