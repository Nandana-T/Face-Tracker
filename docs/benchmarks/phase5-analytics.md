# Phase 5 benchmark – analytics performance and labelled counting evaluation

Harnesses: `backend/benchmarks/benchmark_analytics.py` (scale) and `backend/benchmarks/evaluate_unique_count.py` (ground truth). Raw results: `datasets/evidence/analytics-perf/`, `datasets/evidence/unique-count/` (git-ignored).
Machine: i5-1240P, CPU only, embedded local PostgreSQL 18. **Never the hosted database; never real visitor data.**

## 1. Query latency as the data grows (synthetic rows, isolated schema, median of 7)
| Visits (events = 2×) | Overview all-time | Overview date range | Overview 1 source | Overview 1 session | Timeline day | Timeline hour (range) | Sources | Sessions page | Coverage | Recent | Reconcile (full recount) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1,000 | 10.9 ms | 5.8 | 6.8 | 6.3 | 7.0 | 2.0 | 4.0 | 6.2 | 1.7 | 1.6 | 55 ms |
| 10,000 | 72 ms | 46 | 26 | 9.8 | 55 | 35 | 46 | 40 | 3.3 | 2.1 | 246 ms |
| 100,000 | 664 ms | 121 | 254 | 45 | 670 | 66 | 662 | 464 | 18 | 1.9 | 2.07 s |
* Growth is linear (a scan of the visits/events in scope); filtered views benefit from the existing `entry_time`, `session_id` indexes (a date-ranged returning-visitor join executes in 25 ms at 100 k; `EXPLAIN` in the JSON). Consolidating the overview's count queries from 8 to 5 statements improved small sizes by ~30 % and left the 100 k all-time scan unchanged. **No index or migration was added**: the measurement does not justify one at the target scale. If all-time dashboards over ≫ 100 k visits matter, add daily rollups (not Redis).
* Load time of the synthetic data: 100 k visits + 200 k events + 20 k associations in 34 s. Python memory growth while querying: ≤ 5 MB. Full-recount reconciliation stayed consistent at every size.
* Notice delivery after the database commit: **5.3 ms** (in-process TestClient). Dashboard first figure after navigation: **≈ 1.0 s** on the local stack right after an application restart (Playwright, real API; includes the first-request warm-up). Database load and CPU of analytics were not profiled separately: the queries above are the cost, and the live socket carries only a few bytes per notice and no frames.
* Not measured: hosted-database latency, concurrent dashboards, soak tests.

## 2. Labelled unique-identity counting (LFW identities, real pipeline, synthetic video)
Labels are LFW verified identities, a different photograph per appearance; 14 people, 23 clips, 25 appearances (first appearances, returns with other photographs, a simultaneous pair twice, small faces, mask-like occlusion). Real detector, tracker, landmarks, SFace, identity manager, visit lifecycle and analytics on PostgreSQL; the video is still photographs shown full frame (optimistic).
| | |
|---|---|
| Ground-truth unique people | 14 |
| Registered identities / analytics unique visitors | 14 / 14 (absolute error **0**, but see next row) |
| Correctly registered (one identity, no merge) | 12 |
| Missed | 1 (occluded face: 0/2 appearances resolved) |
| Duplicate registrations | 1 (small face registered twice) |
| Incorrect merges | 0 |
| Resolved appearances | normal 20/21 · small 2/2 · occluded 0/2 |
| Returning: ground truth / analytics | 11 / 8 (first appearance unresolved ⇒ later counted as new; a duplicated person is never "returning") |
| Visits / labelled appearances | 22 / 25 |
**The zero count error is a coincidence of one miss and one duplicate cancelling.** Wrong merges (the worst kind) did not occur. These figures say nothing about the hackathon footage, which has no person-level labels; there, only operational diagnostics exist (74 confirmed tracks, 9 resolved to an identity in Phase 4).
