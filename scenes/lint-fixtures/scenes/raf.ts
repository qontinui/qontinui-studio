// expect: no-restricted-globals
export const tick = (cb: () => void) => requestAnimationFrame(cb);
