// expect: no-restricted-syntax :: CSS transitions/animations run on the wall clock
export const S = () => <div style={{ "-webkit-transition": "x" } as object} />;
