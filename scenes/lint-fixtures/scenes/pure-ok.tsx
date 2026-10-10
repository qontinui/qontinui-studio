// expect: none
import { useMemo } from "react";
export function S({ frame }: { frame: number }) {
  const x = useMemo(() => Math.round(frame * 2), [frame]);
  return <div style={{ opacity: Math.min(1, x / 10) }}>{x}</div>;
}
