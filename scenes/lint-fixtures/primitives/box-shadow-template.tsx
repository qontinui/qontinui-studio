// expect: no-restricted-syntax :: boxShadow in scenes must be solidRings
export const S = ({ e }: { e: number }) => <div style={{ boxShadow: e > 0 ? `0 0 ${e}px red` : "none" }} />;
