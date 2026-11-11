const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : dateTime.format(d);
}

/** "3 min ago" style text; falls back to the absolute time for old dates. */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)} d ago`;
  return formatDateTime(iso);
}

export const SORT_LABEL = {
  last_seen: "Last seen",
  first_seen: "First seen",
  number: "Visitor number",
} as const;

const dateTimeSeconds = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

/** With seconds - event timestamps matter at that resolution. */
export function formatDateTimeSeconds(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : dateTimeSeconds.format(d);
}
