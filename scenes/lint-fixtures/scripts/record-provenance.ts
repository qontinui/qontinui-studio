// expect: no-restricted-syntax :: recordReplayProvenance may be used only by fromBuildRecord
import { recordReplayProvenance } from "../src/api/trace/provenance";
export const r = recordReplayProvenance;
