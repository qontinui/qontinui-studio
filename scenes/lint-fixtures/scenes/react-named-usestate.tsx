// expect: no-restricted-imports :: 'useState' import from 'react' is restricted
import { useState } from "react";
export function S() {
  const [n] = useState(0);
  return <div>{n}</div>;
}
