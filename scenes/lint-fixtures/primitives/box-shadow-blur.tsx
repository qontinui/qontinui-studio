// expect: no-restricted-syntax :: boxShadow in scenes must be solidRings
export const S = () => <div style={{ boxShadow: "0 0 24px 4px red" }} />;
