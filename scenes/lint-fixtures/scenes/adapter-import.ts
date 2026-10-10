// expect: no-restricted-imports :: Only src/cli may load an adapter
import { RemotionAdapter } from "../../adapters/remotion/render";
export const A = RemotionAdapter;
