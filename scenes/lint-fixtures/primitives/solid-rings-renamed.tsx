// expect: no-restricted-syntax :: solidRings must be the one imported from src/api/glow
import { glowRings as solidRings } from "../api/glow";
export const S = () => <div style={{ boxShadow: solidRings(8, 1, "red") }} />;
