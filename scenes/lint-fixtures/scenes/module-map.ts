// expect: no-restricted-syntax :: No module-level mutable containers
const cache = new Map<string, number>();
export const get = (k: string) => cache.get(k);
