// expect: no-restricted-imports :: Only src/cli may load an adapter, and only by dynamic import()
import { HyperFramesAdapter } from "../adapters/hyperframes/render";
export const A = HyperFramesAdapter;
