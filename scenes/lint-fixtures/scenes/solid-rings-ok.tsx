// expect: none
import { glowRings, solidRings } from "../../api";
export const S = ({ e }: { e: number }) => <div style={{ boxShadow: solidRings(glowRings(16, e, "red")) }} />;
