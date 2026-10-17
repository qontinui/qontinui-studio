// expect: no-restricted-globals :: Unexpected use of 'Date'
// expect: no-restricted-properties :: 'Date.now' is restricted
export const t = () => Date.now();
