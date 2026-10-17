/**
 * Deterministic text formatting for on-screen labels. Everything is UTC and
 * locale-free, so a render never depends on the machine's timezone or locale.
 */

/** "2026-09-01" (UTC) from an ISO timestamp or epoch ms. */
export function formatDate(value: string | number): string {
  const ms = typeof value === "number" ? value : Date.parse(value);
  return new Date(ms).toISOString().slice(0, 10);
}

/** "2026-09-01 14:05 UTC" from an ISO timestamp or epoch ms. */
export function formatDateTime(value: string | number): string {
  const ms = typeof value === "number" ? value : Date.parse(value);
  const iso = new Date(ms).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/** "14:05 UTC" from an ISO timestamp or epoch ms. */
export function formatTime(value: string | number): string {
  const ms = typeof value === "number" ? value : Date.parse(value);
  return `${new Date(ms).toISOString().slice(11, 16)} UTC`;
}

/** Compact elapsed duration: "45m", "3h 20m", "2d 4h". Floors to minutes. */
export function formatElapsed(ms: number): string {
  const totalMinutes = Math.max(0, Math.floor(ms / 60_000));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

/** The on-screen mode line required by plan D2. */
export function modeLine(meta: {
  mode: "illustrative" | "replay";
  source: { kind: string; ref: string };
  date_range?: { from: string; to: string } | undefined;
}): { badge: string; detail: string } {
  if (meta.mode === "replay") {
    const range = meta.date_range
      ? `${formatDate(meta.date_range.from)} – ${formatDate(meta.date_range.to)}`
      : "date range missing";
    return { badge: "REPLAY", detail: `${range} · source: ${meta.source.ref}` };
  }
  return { badge: "ILLUSTRATIVE", detail: "Synthetic trace · not a real build" };
}
