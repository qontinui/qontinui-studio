// expect: no-new-func :: The Function constructor is eval
export const f = new Function("return 1");
