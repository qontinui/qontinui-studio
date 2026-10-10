// expect: no-restricted-syntax :: No SVG filters in scenes
export const S = () => (
  <svg>
    <feGaussianBlur stdDeviation="3" />
  </svg>
);
