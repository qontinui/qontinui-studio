// expect: no-restricted-syntax :: Import React hooks by name
// expect: no-restricted-syntax :: no state, refs or effects
import React from "react";
export function S() {
  React.useEffect(() => undefined);
  return null;
}
