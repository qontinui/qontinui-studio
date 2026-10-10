// expect: no-restricted-syntax :: Use Math only as Math.<fn>
const M = Math;
export const r = () => M.random();
