/**
 * One object URL per Blob, created lazily and revoked when the Blob is garbage-collected.
 *
 * Why not create the URL in a component and revoke it in an effect cleanup? React StrictMode (dev)
 * mounts, unmounts and re-mounts effects: the simulated unmount revoked a URL that was created during
 * render, leaving a broken image whenever the data was already cached. Tying the URL's lifetime to the
 * Blob's (which the query cache owns) avoids that and still never leaks beyond the cache entry.
 */
const urls = new WeakMap<Blob, string>();
const registry =
  typeof FinalizationRegistry === "function"
    ? new FinalizationRegistry<string>((url) => URL.revokeObjectURL(url))
    : null;

export function objectUrl(blob: Blob): string {
  let url = urls.get(blob);
  if (url === undefined) {
    url = URL.createObjectURL(blob);
    urls.set(blob, url);
    registry?.register(blob, url);
  }
  return url;
}
