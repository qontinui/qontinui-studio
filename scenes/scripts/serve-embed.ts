/**
 * Serve the embed test page for a human: `pnpm embed:serve [port]`, then open
 * http://127.0.0.1:<port>/?scene=s1-plan-lifecycle&aspect=16:9&autoplay=1
 * (see tests/embed/page.tsx for the parameters).
 */
import { serveEmbedPage } from "../tests/embed/serve";

const port = Number(process.argv[2] ?? 4173);
const server = await serveEmbedPage(port);
process.stdout.write(`embed page: ${server.url}/?scene=s1-plan-lifecycle&aspect=16:9&autoplay=1\n`);
