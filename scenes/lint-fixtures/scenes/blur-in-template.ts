// expect: no-restricted-syntax :: No blur()/drop-shadow() in scenes
export const css = (r: number) => `drop-shadow(0 0 ${r}px red)`;
