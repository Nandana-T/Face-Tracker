r"""Non-destructive smoke test for a DEPLOYED (or local production-like) stack.

    set KATOMARAN_API=https://<your-api>.onrender.com/api/v1
    set KATOMARAN_ADMIN_PASSWORD=...            (never put it on the command line / in the repo)
    set KATOMARAN_FRONTEND=https://<your-app>.vercel.app      (optional)
    python scripts\smoke_deployed.py [--require-worker] [--cacert path]

Read-only: it never starts a session, never registers a visitor and never writes data. Checks
  API   health, readiness (database), anonymous access refused, login, CORS preflight from the frontend
        origin (allowed) and from a foreign origin (not allowed), single-use stream ticket, sources, the
        worker connection (with --require-worker), migration revision, analytics + reconciliation, image
        endpoints private, no secret-looking values in /config and /system/status
  WEB   index served over HTTPS, a deep link (SPA rewrite) returns the app, the JS bundle contains no
        server-side secret names, security-relevant headers noted
Exit code 0 only when every check passes. Output never contains the password or tokens.
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import sys
import time

import httpx

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-worker", action="store_true")
    ap.add_argument("--cacert", default=None, help="CA/self-signed certificate for a local TLS stack")
    args = ap.parse_args()
    api = os.environ.get("KATOMARAN_API", "").rstrip("/")
    password = os.environ.get("KATOMARAN_ADMIN_PASSWORD", "")
    web = os.environ.get("KATOMARAN_FRONTEND", "").rstrip("/")
    if not api:
        print("Set KATOMARAN_API (e.g. https://host/api/v1)", file=sys.stderr)
        return 2
    verify: bool | ssl.SSLContext = ssl.create_default_context(cafile=args.cacert) if args.cacert else True
    c = httpx.Client(base_url=api, verify=verify, timeout=30)

    t = time.perf_counter()
    r = c.get("/health")
    check("health", r.status_code == 200, f"{(time.perf_counter() - t) * 1000:.0f} ms (first call, may include a cold start)")
    t = time.perf_counter()
    c.get("/health")
    check("health (warm round trip)", True, f"{(time.perf_counter() - t) * 1000:.0f} ms")
    ready = c.get("/ready").json()
    check("readiness: database reachable", ready.get("status") == "ready", str(ready.get("checks", {}).get("database", {}).get("status")))
    check("anonymous access refused", c.get("/sources").status_code == 401 and c.get("/analytics/overview").status_code == 401 and c.get("/events").status_code == 401)
    if web:
        pre = c.options("/sources", headers={"Origin": web, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
        check("CORS allows the frontend origin", pre.headers.get("access-control-allow-origin") == web, f"HTTP {pre.status_code}")
        bad = c.options("/sources", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        check("CORS refuses a foreign origin", bad.headers.get("access-control-allow-origin") in (None, ""))

    if not password:
        check("login (skipped: KATOMARAN_ADMIN_PASSWORD not set)", False, "authenticated checks need the password")
    else:
        lr = c.post("/auth/login", json={"password": password})
        check("login", lr.status_code == 200)
        if lr.status_code == 200:
            h = {"Authorization": f"Bearer {lr.json()['access_token']}"}
            stat = c.get("/system/status", headers=h)
            rev = stat.json()["database"]["migration_revision"] if stat.status_code == 200 else None
            check("migration revision is 0004", rev == "0004", f"revision={rev}")
            blob = c.get("/config", headers=h).text + stat.text
            check("no secret-looking values in config/status", not re.search(r"postgres(ql)?(\+\w+)?://|service_role|eyJ[A-Za-z0-9_-]{20,}|password", blob, re.I))
            wrong = c.post("/auth/login", json={"password": password + "x"})
            check("a wrong password is refused", wrong.status_code == 401)
            src = c.get("/sources", headers=h).json()
            check("sources listed; no server paths or URLs exposed", bool(src["sources"]) and not re.search(r"rtsp://|[A-Za-z]:\\\\|/home/|/app/", str(src)))
            check("worker connected" if args.require_worker else "worker status reported", (src["worker"]["connected"] or not args.require_worker), f"connected={src['worker']['connected']}")
            tk = c.post("/analytics/stream-ticket", headers=h)
            check("single-use stream ticket issued", tk.status_code == 200 and tk.json().get("expires_in", 0) > 0)
            ov = c.get("/analytics/overview", headers=h)
            check("analytics overview", ov.status_code == 200 and "registered_visitors" in ov.json())
            rec = c.get("/analytics/reconciliation", headers=h)
            check("count reconciliation (read-only) is consistent", rec.status_code == 200 and rec.json()["consistent"], f"errors={rec.json().get('errors')} warnings={rec.json().get('warnings')}")
            eh = c.get("/events/health", headers=h).json()
            check("event persistence healthy (no dead deliveries)", eh.get("enabled") and eh["events_by_persistence"].get("DEAD", 0) == 0)
            ev = c.get("/events?limit=1", headers=h).json()
            if ev["items"]:
                eid = ev["items"][0]["id"]
                check("evidence image: 401 without a token", c.get(f"/events/{eid}/image").status_code == 401)
                im = c.get(f"/events/{eid}/image", headers=h)
                check("evidence image served privately", im.status_code in (200, 404) and im.headers.get("cache-control", "") in ("private, no-store", "") or im.status_code == 404)

    if web:
        wc = httpx.Client(timeout=30, follow_redirects=True)
        idx = wc.get(web + "/")
        check("frontend index over HTTPS", idx.status_code == 200 and "<div id=\"root\"" in idx.text and web.startswith("https://"))
        deep = wc.get(web + "/analytics")
        check("SPA deep link works (rewrite)", deep.status_code == 200 and "<div id=\"root\"" in deep.text)
        js = [m for m in re.findall(r'src="(/assets/[^"]+\.js)"', idx.text)]
        bundle = "".join(wc.get(web + j).text for j in js[:3])
        check("bundle holds no server-side secret names", not re.search(r"SERVICE_ROLE|WORKER_TOKEN|ADMIN_PASSWORD|AUTH_SECRET|service_role", bundle))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
