// expect: none
import { glowRings, solidRings } from "../api/glow";
export const S = ({ e }: { e: number }) => <div style={{ boxShadow: solidRings(glowRings(16, e, "red")) }} />;
