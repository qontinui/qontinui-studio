// expect: no-restricted-syntax
export const S = () => (
  <svg>
    <animate attributeName="x" dur="1s" />
  </svg>
);
