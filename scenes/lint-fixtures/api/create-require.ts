// expect: no-restricted-imports :: 'node:module' import is restricted
// expect: no-restricted-syntax :: createRequire outside src/adapters
import { createRequire } from "node:module";
export const req = createRequire(import.meta.url);
