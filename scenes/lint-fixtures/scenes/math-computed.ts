// expect: no-restricted-properties :: 'Math.random' is restricted
// expect: no-restricted-syntax :: Use Math only as Math.<fn>
export const r = () => Math["random"]();
