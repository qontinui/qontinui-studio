// expect: no-restricted-imports
import { useState } from "react";
export function S() {
  const [n] = useState(0);
  return <div>{n}</div>;
}
