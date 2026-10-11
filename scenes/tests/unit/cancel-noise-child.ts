/**
 * Child process for adapter-lifecycle.test.ts: after a cancelled adapter's
 * close() has installed the cancel-noise listener, reject one promise either
 * inside that adapter's render context or outside it (the caller's own), and
 * see what the process does. Prints "alive" if it survives to the end.
 *
 *   node [--unhandled-rejections=<mode>] --import tsx cancel-noise-child.ts inside|outside
 */
import { cancelNoiseForTests } from "../../src/adapters/remotion/render";

const where = process.argv[2];
const adapter = {};
cancelNoiseForTests.listen(adapter);
const epipe = () => Promise.reject(Object.assign(new Error("write EPIPE"), { code: "EPIPE" }));
if (where === "inside") cancelNoiseForTests.runAsRender(adapter, () => setTimeout(() => void epipe(), 5));
else setTimeout(() => void epipe(), 5);
setTimeout(() => process.stdout.write("alive\n"), 200);
