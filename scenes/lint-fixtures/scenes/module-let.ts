// expect: no-restricted-syntax :: No module-level mutable state
let n = 0;
export const next = () => ++n;
