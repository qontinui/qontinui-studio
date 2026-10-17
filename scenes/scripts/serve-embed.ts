/**
 * Serve the embed test page for a human: `pnpm embed:serve [port]`, then open
 * http://127.0.0.1:<port>/?scene=s1-plan-lifecycle&aspect=16:9&autoplay=1
 * (see tests/embed/page.tsx for the parameters). Replay mode is refused here:
 * the test-only replay hook works only when vitest builds the page.
 */
import { serveEmbedPage } from "../tests/embed/serve";

const port = Number(process.argv[2] ?? 4173);
if (!Number.isInteger(port) || port < 0 || port > 65535) {
  process.stderr.write(`usage: pnpm embed:serve [port]; "${process.argv[2]}" is not a port\n`);
  process.exit(2);
}
const server = await serveEmbedPage(port);
process.stdout.write(`embed page: ${server.url}/?scene=s1-plan-lifecycle&aspect=16:9&autoplay=1\n`);
