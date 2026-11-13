# Sample output (synthetic / public-domain fixture)

Produced by `scripts/make_sample_output.py` from the controlled end-to-end run. The only face is the **public-domain NASA portrait** used as a test fixture (`backend/tests/fixtures/astronaut_nasa_public_domain.png`). **No hackathon footage, no real visitor and no credential appears here**; real-footage evidence stays private (git-ignored) and is described only in aggregate in the reports.

* `analytics-overview.json` - counts (identities, visits, entry/terminal events, active vs stale) with their definitions
* `reconciliation.json` - the read-only independent recount
* `visits-and-events.json` - sample visits with their entry/exit events
* `events-log-excerpt.jsonl` - the matching `events.log` lines (delivery attempt, persistence outcome)
* `images/` - timestamped entry/exit crops (entry = best observation; exit = last good observation, not a new capture)
* `screenshots/` - real dashboard screenshots (Overview, Visitors gallery, Analytics) captured by `frontend/e2e/capture-evidence.spec.ts`
* `visitor-records.json`, `visit-records.json`, `event-records.json`, `analytics-dashboard-overview.json` - API responses from that same run; `events-log-dashboard-run.jsonl` - the matching `events.log` lines
