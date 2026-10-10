/**
 * Download the Chrome Headless Shell build pinned by the exact
 * @remotion/renderer version into node_modules/.remotion (CI caches it).
 */
import { ensureBrowser } from "@remotion/renderer";

const status = await ensureBrowser({ logLevel: "info" });
process.stdout.write(`browser: ${JSON.stringify(status)}\n`);
