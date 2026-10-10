// expect: no-restricted-syntax :: Computed property keys are not allowed
const key = "trans" + "ition";
export const S = () => <div style={{ [key]: "opacity 1s" }} />;
