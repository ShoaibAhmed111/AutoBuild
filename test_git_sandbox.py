import subprocess
import tempfile
from pathlib import Path

from git_sandbox import GitSandbox


def sh(cmd, cwd):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)


def test_stash_is_created_and_restored_on_discard():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "file.txt").write_text("committed content\n")
        sh("git init -q", root)
        sh("git config user.email test@example.com", root)
        sh("git config user.name Test", root)
        sh("git add . && git commit -q -m init", root)

        # Simulate the user having uncommitted work when Auto Build starts.
        (root / "file.txt").write_text("USER'S UNCOMMITTED WORK\n")

        sandbox = GitSandbox(str(root))
        sandbox.create_session_branch()
        assert sandbox._stashed is True, "dirty tree should have been stashed"

        # working tree should now be clean (the dirty change was stashed away)
        status = sh("git status --porcelain", root).stdout.strip()
        assert status == "", f"expected clean tree after stash, got: {status!r}"

        # agent makes a change on the session branch
        (root / "file.txt").write_text("AGENT CHANGE\n")
        sh("git add . && git commit -q -m agent_change", root)

        ok, msg = sandbox.discard()
        assert ok is True
        assert sandbox._stashed is False, "stash flag should be cleared after restore"

        # user's original uncommitted work must be restored, not stranded
        content = (root / "file.txt").read_text()
        assert content == "USER'S UNCOMMITTED WORK\n", f"user's stashed work was not restored: {content!r}"

        stash_list = sh("git stash list", root).stdout.strip()
        assert stash_list == "", f"stash should be empty after pop, got: {stash_list!r}"
        print("PASS: dirty-tree stash is created before branching and correctly restored after discard (old bug: stash was pushed but never popped)")


def test_merge_checks_real_exit_code_on_conflict():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "file.txt").write_text("line one\n")
        sh("git init -q", root)
        sh("git config user.email test@example.com", root)
        sh("git config user.name Test", root)
        sh("git add . && git commit -q -m init", root)

        sandbox = GitSandbox(str(root))
        sandbox.create_session_branch()

        # agent changes the file and commits on the session branch
        (root / "file.txt").write_text("agent's version\n")
        sh("git add . && git commit -q -m agent_change", root)

        # meanwhile, a conflicting change lands on the original branch
        sh(f"git checkout -q {sandbox.original_branch}", root)
        (root / "file.txt").write_text("someone else's conflicting version\n")
        sh("git add . && git commit -q -m conflicting_change", root)

        ok, msg = sandbox.merge()
        assert ok is False, "merge() must report failure on a real conflict, not unconditional success"
        assert "conflict" in msg.lower() or "fail" in msg.lower()
        # session branch must still exist for recovery since merge failed
        branches = sh("git branch", root).stdout
        assert sandbox.active_branch in branches
        print("PASS: merge() correctly detects and reports a real conflict instead of always claiming success (old bug: unconditional 'Successfully merged')")


def test_merge_succeeds_and_cleans_up_branch():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "file.txt").write_text("line one\n")
        sh("git init -q", root)
        sh("git config user.email test@example.com", root)
        sh("git config user.name Test", root)
        sh("git add . && git commit -q -m init", root)

        sandbox = GitSandbox(str(root))
        sandbox.create_session_branch()
        (root / "new_file.txt").write_text("new content\n")
        sh("git add . && git commit -q -m agent_change", root)

        ok, msg = sandbox.merge()
        assert ok is True, msg
        assert (root / "new_file.txt").exists()
        branches = sh("git branch", root).stdout
        assert sandbox.active_branch not in branches, "merged session branch should be cleaned up"
        print("PASS: a clean merge succeeds, is reported accurately, and the session branch is cleaned up")


def test_commit_then_discard_actually_removes_the_change():
    """The critical missing piece found during integration testing: nothing
    committed agent changes to the session branch, so merge/discard were
    largely symbolic (uncommitted files ride along across any checkout
    regardless of branch). This confirms commit_changes() + discard()
    together actually remove the agent's work when the user says no."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "file.txt").write_text("original\n")
        sh("git init -q", root)
        sh("git config user.email test@example.com", root)
        sh("git config user.name Test", root)
        sh("git add . && git commit -q -m init", root)

        sandbox = GitSandbox(str(root))
        sandbox.create_session_branch()

        (root / "new_agent_file.txt").write_text("agent wrote this\n")
        ok, msg = sandbox.commit_changes("agent change")
        assert ok, msg

        diff = sandbox.diff_against_original()
        assert "new_agent_file.txt" in diff

        ok, msg = sandbox.discard()
        assert ok
        assert not (root / "new_agent_file.txt").exists(), "discard() should remove the agent's committed file entirely"
        print("PASS: commit_changes() + discard() genuinely removes the agent's committed work (old flow never committed, so 'discard' didn't actually undo anything)")


if __name__ == "__main__":
    test_stash_is_created_and_restored_on_discard()
    test_merge_checks_real_exit_code_on_conflict()
    test_merge_succeeds_and_cleans_up_branch()
    test_commit_then_discard_actually_removes_the_change()
    print("\nALL GIT SANDBOX TESTS PASSED")
