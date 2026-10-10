// expect: no-restricted-syntax :: Computed access on process outside src/adapters
export const mod = () => (globalThis.process as unknown as Record<string, unknown>)["getBuiltin" + "Module"];
