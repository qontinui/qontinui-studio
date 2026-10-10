// expect: no-restricted-globals :: Unexpected use of 'requestAnimationFrame'
export const tick = (cb: () => void) => requestAnimationFrame(cb);
