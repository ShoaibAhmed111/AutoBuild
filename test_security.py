import os
import tempfile
from pathlib import Path

from security import get_or_create_auth_token, is_path_allowed, get_allowed_roots


def test_token_generated_when_env_unset():
    os.environ.pop("AUTOBUILD_AUTH_TOKEN", None)
    token, generated = get_or_create_auth_token()
    assert generated is True
    assert len(token) > 20
    print("PASS: a random token is generated when AUTOBUILD_AUTH_TOKEN is unset")


def test_token_from_env_when_set():
    os.environ["AUTOBUILD_AUTH_TOKEN"] = "fixed-token-xyz"
    try:
        token, generated = get_or_create_auth_token()
        assert generated is False
        assert token == "fixed-token-xyz"
        print("PASS: AUTOBUILD_AUTH_TOKEN from the environment is used verbatim, not regenerated")
    finally:
        del os.environ["AUTOBUILD_AUTH_TOKEN"]


def test_no_allowlist_configured_allows_with_warning():
    os.environ.pop("AUTOBUILD_ALLOWED_ROOTS", None)
    assert get_allowed_roots() is None
    allowed, reason = is_path_allowed(Path("/tmp/anything_at_all"))
    assert allowed is True
    assert "No AUTOBUILD_ALLOWED_ROOTS configured" in reason
    print("PASS: with no allowlist configured, any path is allowed but with an explicit warning (not silence)")


def test_allowlist_permits_paths_inside_and_rejects_outside():
    with tempfile.TemporaryDirectory() as tmp:
        allowed_root = Path(tmp) / "workspace"
        (allowed_root / "project1").mkdir(parents=True)
        outside_dir = Path(tmp) / "elsewhere"
        outside_dir.mkdir()

        os.environ["AUTOBUILD_ALLOWED_ROOTS"] = str(allowed_root)
        try:
            ok_inside, reason_inside = is_path_allowed(allowed_root / "project1")
            assert ok_inside is True
            assert reason_inside == ""

            ok_outside, reason_outside = is_path_allowed(outside_dir)
            assert ok_outside is False
            assert "outside the configured allowlist" in reason_outside
            print("PASS: a configured allowlist permits paths inside it and rejects paths outside it")
        finally:
            del os.environ["AUTOBUILD_ALLOWED_ROOTS"]


def test_allowlist_supports_multiple_roots():
    with tempfile.TemporaryDirectory() as tmp:
        root_a = Path(tmp) / "a"
        root_b = Path(tmp) / "b"
        root_a.mkdir()
        root_b.mkdir()

        os.environ["AUTOBUILD_ALLOWED_ROOTS"] = f"{root_a}{os.pathsep}{root_b}"
        try:
            roots = get_allowed_roots()
            assert len(roots) == 2
            ok_a, _ = is_path_allowed(root_a / "sub")
            ok_b, _ = is_path_allowed(root_b / "sub")
            assert ok_a and ok_b
            print("PASS: multiple allowed roots (os.pathsep-separated) all permit paths inside them")
        finally:
            del os.environ["AUTOBUILD_ALLOWED_ROOTS"]


if __name__ == "__main__":
    test_token_generated_when_env_unset()
    test_token_from_env_when_set()
    test_no_allowlist_configured_allows_with_warning()
    test_allowlist_permits_paths_inside_and_rejects_outside()
    test_allowlist_supports_multiple_roots()
    print("\nALL SECURITY TESTS PASSED")
