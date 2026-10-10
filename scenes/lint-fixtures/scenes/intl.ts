// expect: no-restricted-globals :: Unexpected use of 'Intl'
export const t = () => new Intl.DateTimeFormat("en").format();
