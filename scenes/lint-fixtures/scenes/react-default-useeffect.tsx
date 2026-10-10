// expect: no-restricted-syntax
import React from "react";
export function S() {
  React.useEffect(() => undefined);
  return null;
}
