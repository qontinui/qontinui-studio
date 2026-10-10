// expect: no-restricted-syntax :: solidRings must be the one imported from src/api/glow
const solidRings = (r: number) => `0 0 ${r}px red`;
export const S = () => <div style={{ boxShadow: solidRings(8) }} />;
