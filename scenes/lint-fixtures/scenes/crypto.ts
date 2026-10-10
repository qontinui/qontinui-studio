// expect: no-restricted-globals :: Unexpected use of 'crypto'
// expect: no-restricted-properties :: 'crypto.randomUUID' is restricted
export const id = () => crypto.randomUUID();
