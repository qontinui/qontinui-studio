// expect: no-eval :: eval can be harmful
export const run = (code: string) => eval(code);
