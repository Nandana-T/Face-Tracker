/**
 * Bearer-token storage for the optional sign-in (see backend app/core/security.py).
 *
 * The token lives in sessionStorage (cleared when the tab closes) with an in-memory fallback.
 * It is an *access* token for this API only; no credentials are ever embedded in the bundle.
 */
const KEY = "katomaran.access-token";
let memory: string | null = null;

export const UNAUTHORIZED_EVENT = "katomaran:unauthorized";
export const authEvents = new EventTarget();

export const authToken = {
  get(): string | null {
    try {
      return sessionStorage.getItem(KEY) ?? memory;
    } catch {
      return memory;
    }
  },
  set(token: string): void {
    memory = token;
    try {
      sessionStorage.setItem(KEY, token);
    } catch {
      /* storage unavailable: memory only */
    }
  },
  clear(): void {
    memory = null;
    try {
      sessionStorage.removeItem(KEY);
    } catch {
      /* ignore */
    }
  },
};

export function notifyUnauthorized(): void {
  authToken.clear();
  authEvents.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}
