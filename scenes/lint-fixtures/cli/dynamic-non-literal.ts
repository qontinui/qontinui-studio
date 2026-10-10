// expect: no-restricted-syntax :: Dynamic import() needs a literal specifier
export const load = (specifier: string) => import(specifier);
