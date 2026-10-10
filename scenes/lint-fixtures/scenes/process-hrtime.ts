// expect: no-restricted-globals :: Unexpected use of 'process'
// expect: no-restricted-properties :: 'process.hrtime' is restricted
export const t = () => process.hrtime.bigint();
