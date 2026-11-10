# ADR 0001 – Modular monolith + separate worker
**Status:** accepted. **Context:** hackathon scope, but inference must not block the API and may run on different hardware.
**Decision:** one FastAPI codebase with strict package boundaries plus a worker entrypoint (`app.workers.video_worker`) in the same repo/package. No Redis/Celery/microservices.
**Consequences:** simple deploys; API and worker communicate through PostgreSQL (and later an event stream). If throughput demands it, the worker can scale out independently without restructuring.
