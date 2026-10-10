// expect: no-restricted-globals
export const t = () => new Intl.DateTimeFormat("en").format();
