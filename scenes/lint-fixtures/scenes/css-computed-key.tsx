// expect: no-restricted-syntax
const key = "trans" + "ition";
export const S = () => <div style={{ [key]: "opacity 1s" }} />;
