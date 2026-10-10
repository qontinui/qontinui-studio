// expect: no-restricted-imports :: Only src/cli may load an adapter, and only by dynamic import()
import { RemotionAdapter } from "../adapters/remotion/render";
export const A = RemotionAdapter;
