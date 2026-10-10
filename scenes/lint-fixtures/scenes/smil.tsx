// expect: no-restricted-syntax :: SVG SMIL elements animate on the wall clock
export const S = () => (
  <svg>
    <animate attributeName="x" dur="1s" />
  </svg>
);
