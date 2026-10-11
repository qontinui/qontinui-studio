/**
 * Child process for adapter-lifecycle.test.ts: fire a render that fails and
 * never await it. Node's default mode must still report it as an unhandled
 * rejection (the adapter's own bookkeeping must not count as handling it).
 *
 *   node --import tsx unawaited-render-child.ts remotion|hyperframes
 */
import { HyperFramesAdapter } from "../../src/adapters/hyperframes/render";
import { RemotionAdapter } from "../../src/adapters/remotion/render";
import { parseTrace } from "../../src/api/trace/schema";
import { s1TraceJson } from "./fixtures";

const engine = process.argv[2];
const adapter =
  engine === "hyperframes"
    ? new HyperFramesAdapter({ docker: "/nonexistent/studio-test-no-docker" })
    : new RemotionAdapter({ serveUrl: "http://127.0.0.1:9/unused", port: 9 });
const trace = parseTrace(s1TraceJson());
// Fails before any engine work: no such scene (Remotion); an output directory
// that cannot be created (HyperFrames; the docker binary is missing too).
void adapter.render({
  scene: engine === "hyperframes" ? "s1-plan-lifecycle" : "no-such-scene",
  trace,
  format: "png",
  frame: 0,
  aspect: "16:9",
  outPath: "/nonexistent/studio-unawaited/out.png",
});
setTimeout(() => process.stdout.write("alive\n"), 500);
