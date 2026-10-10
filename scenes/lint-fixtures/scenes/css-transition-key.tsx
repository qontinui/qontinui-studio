// expect: no-restricted-syntax :: CSS transitions/animations run on the wall clock
export const S = () => <div style={{ transition: "opacity 1s" }} />;
