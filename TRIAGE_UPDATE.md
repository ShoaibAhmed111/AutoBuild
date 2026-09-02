# Auto Build — Three-Tier Triage Update

This adds the thing you actually asked for: not "10/10," but a system that (1) handles small/medium changes cheaply without invoking the full multi-file planning pipeline, (2) still uses the verified plan→write→verify→repair pipeline for real feature work, and (3) refuses to guess on genuinely risky/cross-cutting changes and gives you an instruction sheet instead. All of it is additive on top of the previously-delivered code; nothing from the last round was removed, and all previous tests still pass.

## New file: `triage.py`

Pure heuristic (regex pattern matching), on purpose — classification itself shouldn't cost an LLM call, since burning a call just to decide whether to burn more calls defeats the point. Three tiers:

- **GUIDANCE_ONLY** — matched a high-risk pattern (schema migrations, "refactor the entire X", auth system changes, payments, encryption, etc.). One advisory LLM call produces a structured guidance report (affected files, why, what to change, ordered steps). **Zero file writes** — verified by snapshotting the whole repo before and after and asserting nothing changed.
- **MECHANICAL** — short, single-concern request (color/CSS/typo/rename/comment-type asks) that doesn't reference multiple files. Skips the multi-file planning call entirely; a heuristic keyword-overlap scorer picks the one target file from the repo map, and one write call runs through the *same* static/runtime/repair verification loop as everything else. Verified with a test asserting exactly 1 LLM call happened (if planning had run too, the test's fake LLM would have run out of scripted responses and failed).
- **STANDARD** — everything else: the existing full plan → write → verify → repair pipeline, unchanged.

If the mechanical file-picker can't confidently identify a single file (ties, or no keyword overlap at all), it explicitly falls back to STANDARD rather than guessing — verified by a dedicated test.

## Orchestrator refactor

`run_task()`'s body (write subtasks → static → runtime → command → bounded repair loop) was extracted into a shared `_execute_and_verify()` used by both the STANDARD path (`run_task`) and the new MECHANICAL path (`run_mechanical`), so verification behavior can't drift between the fast path and the full path. A new `run_request()` is the actual front door: it runs triage first, then dispatches to one of the three. `server.py` and `main.py` now call `run_request()` instead of `run_task()` directly.

## UI

Added a guidance panel (distinct from the diff view) that renders the structured guidance text when a request is flagged GUIDANCE_ONLY, plus a "guidance" status badge so it's visually distinct from success/partial/failed. The task log now shows the triage decision itself (`TRIAGE_DECISION: MECHANICAL: ...`) so you can see why something was routed the way it was.

## Honest limitations of this pass

- **Triage is regex, not understanding.** It will misclassify sometimes. A prompt that doesn't hit any risk keyword but is actually sweeping falls through to STANDARD (the safe default) rather than GUIDANCE_ONLY — so the worst-case failure mode of a triage miss is "used the expensive-but-safe path when the cheap path might have worked," not "silently applied something dangerous."
- **The mechanical file-picker is naive token overlap**, not semantic matching. Demonstrated during testing: for "change the submit button color," it correctly found `index.html` (shares the literal word "button") but would *not* reliably prefer a separate `style.css` file over the HTML file, since "btn-primary" in the CSS doesn't literally contain the word "button." For now this mostly self-corrects because the LLM write step still receives the actual file content and can make a reasonable edit within whatever file gets picked — but it's a real, acknowledged weakness, not a solved problem.
- **No caching/dedup of repeated similar requests** yet — every task still re-reads the repo and rebuilds the contract registry from scratch. That's a legitimate next lever if token/time cost still matters at your usage volume.
- **Guidance mode's LLM call still uses the full repo map + contracts in the prompt** — it's cheaper than the full pipeline (one call, no repair loop) but not free. A cheaper "guidance lite" mode using only a filtered subset of the repo map is a reasonable next step if this matters.

## Test coverage added

`test_triage.py` — 4 new tests, all passing: mechanical tier uses exactly 1 LLM call, guidance tier writes zero files (verified via before/after file-content snapshots), standard tier still handles ordinary multi-file feature requests, and mechanical correctly falls back to standard when file selection is ambiguous. Plus a new websocket-level test in `test_server.py` confirming the guidance panel renders correctly over the real WebSocket transport and no files are created.
