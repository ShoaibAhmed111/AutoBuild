# Round 5: Security Hardening + AST-Based Understanding

Both of the two gaps flagged as remaining after the last rating are now addressed for real, with tests. New files: `security.py`, `test_indexer.py`, `test_contract_registry_ast.py`, `test_security.py`.

## Security hardening

**Auth token gate on the WebSocket.** At server startup, if `AUTOBUILD_AUTH_TOKEN` isn't set in the environment, a random token is generated and printed prominently to the server's own console (never embedded in the served HTML — that would defeat the point, since anyone loading the page would get it for free). Every WebSocket connection must send this token on its first `init` action; every other action is refused until authenticated. Set `AUTOBUILD_AUTH_TOKEN` yourself to use a fixed token across restarts.

**Project-path allowlisting.** Set `AUTOBUILD_ALLOWED_ROOTS` (an `os.pathsep`-separated list of directories) to restrict which filesystem paths the server will ever operate on. If unset, the server still works (so this doesn't break anyone who hasn't configured it) — but it logs a loud, explicit warning every time it accepts a path, rather than silently doing nothing. This is the direct fix for the flagged hole: "anyone who can reach the port can point the server at an arbitrary directory."

**Honest scope note**: this is one shared token and a directory allowlist, not enterprise auth — no per-user accounts, no rate limiting, no TLS (that's a reverse proxy's job if you ever expose this beyond localhost). It closes the specific hole that existed, not every conceivable one.

Tests: `test_security.py` (5 tests on the module itself) plus a new `test_auth_gate_rejects_missing_or_wrong_token` in `test_server.py` proving the gate actually rejects bad/missing tokens over a real WebSocket, not just in isolation.

## AST-based codebase understanding

**`indexer.py`** now uses real `ast.parse` for Python (JS/TS/Java remain regex, as before — still an honest limitation). Three concrete, demonstrated improvements over the old line-prefix scanning:
- **Multi-line decorators and signatures** are correctly reconstructed via `ast.unparse()` into clean single lines, regardless of how the original code wrapped them. The old version would have mangled or missed these entirely.
- **Pydantic model fields are now shown with their types** in the repo map — previously invisible; the old indexer only ever showed function/class headers.
- **Graceful fallback**: if a file has invalid Python (mid-edit, or genuinely broken), that one file falls back to the old line-heuristic instead of crashing indexing for the whole repo.

**`verification.py`'s `ContractRegistry`** was rewritten from regex to AST. The most concrete, demonstrable bug this fixes: the old regex was hardcoded to the literal string `"@router."` — a file that named its router variable anything else (`api_router`, `orders_router`, etc., a completely normal real-world pattern) had **every single route in it silently invisible** to the contract registry, meaning static verification would report a false "no such endpoint" for a route that actually exists. AST tracks the real variable name assigned via `X = APIRouter(...)` and matches decorators against it. Also new: `RouteContract.request_field_types` — actual type annotations (`str`, `int`, `Optional[str]`), not just field names, which the old regex-based version couldn't extract at all.

Confirmed non-regression: the full runtime-probe pipeline (schema-derived POST bodies, route discovery) still works end-to-end against the real toy FastAPI project used throughout this project's testing.

## Test totals

43 tests now pass across 10 test files (was 33 last round): the 33 existing ones (all still passing, confirming non-regression) plus `test_security.py` (5), `test_indexer.py` (5, including the multi-line-decorator and non-standard-router-name cases described above).

## What's still not done

- JS/TS/Java parsing is still regex — a real parser for those wasn't in scope this round.
- Auth is one shared token, not per-user; no rate limiting.
- AST parsing still can't resolve genuinely dynamic values (a path or prefix built from a variable/f-string at runtime) — no parsing technique can, since that information doesn't exist until the code actually runs. The registry correctly declines to guess in that case (tested), same principle as before, just enforced via a stronger extraction method.
