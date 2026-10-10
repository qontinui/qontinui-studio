// expect: no-restricted-syntax :: process.getBuiltinModule outside src/adapters
export const mod = () => process.getBuiltinModule("node:module");
