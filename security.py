# security.py
"""
Minimal security hardening for the local web server:

1. Auth token gating the WebSocket -- generated fresh at startup (printed to
   the server's own console/log, never embedded in the served HTML) unless
   AUTOBUILD_AUTH_TOKEN is set in the environment. This follows the same
   pattern Jupyter uses: anyone who can read the server process's own
   stdout/logs can get the token (that's an accepted trust boundary for a
   local dev tool), but anyone who can merely reach the port over the
   network cannot.

2. Project-path allowlisting via AUTOBUILD_ALLOWED_ROOTS (a
   os.pathsep-separated list of directories). If unset, the server still
   works (so this doesn't break existing single-user setups that haven't
   configured it) but loudly logs that it is accepting an arbitrary
   filesystem path -- silence was exactly the problem with the original
   code, and staying silent here would just relocate the same problem.

HONESTY NOTE: this is not enterprise auth. There's one shared token, no
per-user accounts, no rate limiting, no TLS termination (that's the
reverse proxy's job if this is ever exposed beyond localhost). It closes
the specific hole flagged in the original review -- "anyone who can reach
the port can point the server at an arbitrary directory and trigger code
execution there" -- without pretending to be a general-purpose auth system.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional


def get_or_create_auth_token() -> tuple[str, bool]:
    """Returns (token, was_generated). If AUTOBUILD_AUTH_TOKEN is set in the
    environment, uses it (was_generated=False). Otherwise generates a fresh
    random token for this process's lifetime (was_generated=True) -- callers
    MUST print/log this prominently so the user can actually copy it into
    the UI, since it is never sent to the browser automatically."""
    env_token = os.environ.get("AUTOBUILD_AUTH_TOKEN")
    if env_token:
        return env_token, False
    return secrets.token_urlsafe(24), True


def get_allowed_roots() -> Optional[list[Path]]:
    """Returns the configured allowlist of root directories, or None if
    AUTOBUILD_ALLOWED_ROOTS is not set (meaning: no restriction configured,
    caller must decide how to handle that -- see is_path_allowed)."""
    raw = os.environ.get("AUTOBUILD_ALLOWED_ROOTS")
    if not raw:
        return None
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part).resolve())
    return roots or None


def is_path_allowed(path: Path) -> tuple[bool, str]:
    """
    Checks a resolved project path against AUTOBUILD_ALLOWED_ROOTS.

    Returns (allowed, reason):
    - No allowlist configured -> (True, "<warning text>") -- the caller is
      expected to log the warning text, since accepting an unrestricted path
      is exactly the behavior a security-conscious deployment should not do
      silently.
    - Allowlist configured, path is inside (or equal to) one of the allowed
      roots -> (True, "").
    - Allowlist configured, path is outside all allowed roots -> (False,
      "<rejection reason>").
    """
    resolved = path.resolve()
    roots = get_allowed_roots()

    if roots is None:
        return (
            True,
            f"No AUTOBUILD_ALLOWED_ROOTS configured -- accepting arbitrary path "
            f"'{resolved}' unrestricted. Set AUTOBUILD_ALLOWED_ROOTS to restrict "
            f"which directories this server will operate on.",
        )

    for root in roots:
        try:
            resolved.relative_to(root)
            return True, ""
        except ValueError:
            continue

    allowed_list = ", ".join(str(r) for r in roots)
    return (
        False,
        f"Path '{resolved}' is outside the configured allowlist ({allowed_list}). "
        f"Refusing to initialize a project outside allowed roots.",
    )
