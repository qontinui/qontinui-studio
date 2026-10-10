// expect: no-restricted-syntax :: solidRings must be the one imported from src/api/glow
import { solidRings } from "./my-glow";
export const S = () => <div style={{ boxShadow: solidRings([]) }} />;
