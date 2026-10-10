// expect: no-restricted-globals
export const t = () => process.hrtime.bigint();
