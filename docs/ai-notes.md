# AI planning and implementation notes

This project was built with an AI coding assistant (Claude Code) working from the owner's phase prompts. These notes record how
the assistant was used and what was verified, so reviewers can judge the work rather than trust it.

## Working method
1. Inspect the repository and existing contracts before changing anything (Phase 1 began with the vision interfaces, config schema,
   worker entrypoint, test infrastructure and the Phase 0 frontend components).
2. Decide with evidence: model options were researched (model cards, file metadata – which exposed the licence conflict) and the
   chosen weights were pinned by commit and SHA-256.
3. Implement vertically (config → vision → pipeline → worker → API → UI) and test each layer at the lowest level that can fail.
4. Prefer measured facts to claims: the benchmark script produced every performance number; the report is rendered from its JSON.
5. Treat every surprising result as a possible defect. Several real defects were found this way (see `phase-1-report.md` §8).

## Where the assistant's output was checked
* Real detector on a real photograph and a generated video; geometry verified against known face positions, including through the
  browser canvas at pixel level.
* Tests were mutation-checked where a regression test was added (the StrictMode canvas test was run with the fix removed and fails).
* Tooling failures were diagnosed from evidence rather than worked around blindly (e.g. a Playwright shutdown hang was traced to
  Windows process-tree termination and replaced with explicit process management).

## Decisions that need a human
* Licence of the face model (ADR 0007), rotation of the shared database credential (ADR 0010), authentication before any public
  exposure (`privacy-and-security.md`), and supplying consented real footage / an RTSP camera for accuracy and camera testing.

## Known limits of the AI-assisted process
Web-sourced facts (model-card metrics, vendor GPU latencies, provider free-tier terms) were read from search summaries and model cards
and were **not independently verified**. The assistant could not run Docker, a real RTSP camera, a GPU, or the hosted database here.
