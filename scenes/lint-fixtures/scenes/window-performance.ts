// expect: no-restricted-globals :: Unexpected use of 'window'
export const t = () => window.performance.now();
