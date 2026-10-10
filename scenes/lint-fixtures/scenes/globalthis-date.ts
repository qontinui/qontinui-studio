// expect: no-restricted-globals :: Unexpected use of 'globalThis'
export const t = () => globalThis.Date.now();
