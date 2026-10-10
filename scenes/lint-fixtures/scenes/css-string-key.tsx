// expect: no-restricted-syntax
export const S = () => <div style={{ "-webkit-transition": "x" } as object} />;
