// expect: no-restricted-imports :: Only src/cli may load an adapter
import { HyperFramesAdapter } from "../../adapters/hyperframes/render";
export const A = HyperFramesAdapter;
