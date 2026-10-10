// expect: no-restricted-syntax :: No blur or filter in scenes
// expect: no-restricted-syntax :: No blur()/drop-shadow() in scenes
export const S = () => <div style={{ filter: "blur(4px)" }} />;
