// expect: no-restricted-syntax :: No .constructor access in scenes
export const F = (x: object) => (x as Record<string, unknown>)["constructor"];
