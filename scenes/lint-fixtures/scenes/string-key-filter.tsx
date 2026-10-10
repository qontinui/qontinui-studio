// expect: no-restricted-syntax :: No blur or filter in scenes
export const S = () => <div style={{ "backdrop-filter": "none" } as object} />;
