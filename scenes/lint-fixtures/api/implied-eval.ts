// expect: no-implied-eval :: Implied eval
export const later = () => setTimeout("doIt()", 10);
