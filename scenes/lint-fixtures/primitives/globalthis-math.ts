// expect: no-restricted-globals :: Unexpected use of 'globalThis'
// expect: no-restricted-syntax :: Use Math only as Math.<fn>
export const r = () => globalThis.Math.random();
