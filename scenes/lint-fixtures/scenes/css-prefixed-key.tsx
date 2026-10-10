// expect: no-restricted-syntax :: CSS transitions/animations run on the wall clock
export const S = () => <div style={{ WebkitAnimation: "spin 1s" }} />;
