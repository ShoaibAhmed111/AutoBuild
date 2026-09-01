import subprocess
from pathlib import Path
from datetime import datetime


class GitSandbox:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.original_branch = "main"
        self.active_branch = None
        self._stashed = False
        self._ensure_repo()

    def _run(self, cmd: str) -> tuple[int, str]:
        """Runs a shell command and returns (exit_code, output)."""
        try:
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=self.repo_path)
            return res.returncode, (res.stdout.strip() or res.stderr.strip())
        except Exception as e:
            return 1, str(e)

    def _ensure_repo(self):
        code, _ = self._run("git status")
        if code != 0:
            print("[Git] Initializing new repository...")
            self._run("git init")
            self._run("git add .")
            self._run('git commit -m "Initial commit"')

        _, out = self._run("git branch --show-current")
        self.original_branch = out if out else "main"

    def create_session_branch(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.active_branch = f"agent-session-{timestamp}"

        # Check for dirty working tree before branching (safety mechanism)
        code, out = self._run("git status --porcelain")
        if out:
            print("[Git] Dirty working tree detected. Stashing changes safely.")
            # FIXED: Double quotes for Windows cmd.exe argument parsing
            stash_code, stash_out = self._run('git stash push -m "Auto-stash before agent session"')
            self._stashed = stash_code == 0
            if not self._stashed:
                print(f"[Git WARNING] Failed to stash existing changes: {stash_out}")
                print("[Git WARNING] Proceeding anyway -- your uncommitted changes will ride along into the session branch.")

        self._run(f"git checkout -b {self.active_branch}")
        print(f"[Git] Created isolated session branch: {self.active_branch}")

    def _restore_stash_if_any(self):
        """Pops the auto-stash created in create_session_branch(), if any."""
        if not self._stashed:
            return
        code, out = self._run("git stash pop")
        if code == 0:
            print("[Git] Restored your original uncommitted changes (git stash pop).")
        else:
            print(f"[Git ERROR] Could not automatically restore your stashed changes: {out}")
            print("[Git] Your original changes are still safe -- run `git stash list` and `git stash pop` manually.")
        self._stashed = False

    def commit_changes(self, message: str) -> tuple[bool, str]:
        """Commits all current changes onto the active session branch."""
        self._run("git add -A")
        code, out = self._run(f'git commit -m "{message}"')
        if code == 0:
            return True, "Changes committed to session branch."
        if "nothing to commit" in out.lower():
            return True, "Nothing to commit (no files were changed)."
        return False, f"Commit failed: {out}"

    def diff_against_original(self) -> str:
        """Diff of the session branch's committed changes against the original branch."""
        _, out = self._run(f"git diff {self.original_branch}...{self.active_branch}")
        return out

    def merge(self) -> tuple[bool, str]:
        """Non-interactive merge of the session branch into the original branch."""
        self._run(f"git checkout {self.original_branch}")
        ret_code, out = self._run(f"git merge {self.active_branch}")
        if ret_code == 0:
            self._run(f"git branch -d {self.active_branch}")
            self._restore_stash_if_any()
            return True, f"Successfully merged {self.active_branch} into {self.original_branch}."
        return (
            False,
            f"Merge failed or resulted in conflicts:\n{out}\n"
            f"You must resolve conflicts manually on branch {self.original_branch}. "
            f"The session branch '{self.active_branch}' was left intact for recovery.",
        )

    def discard(self) -> tuple[bool, str]:
        """Non-interactive discard of the session branch."""
        self._run(f"git checkout {self.original_branch}")
        self._run(f"git branch -D {self.active_branch}")
        self._restore_stash_if_any()
        return True, f"Changes on {self.active_branch} discarded. Returned to {self.original_branch}."

    def finalize(self):
        """CLI-only interactive wrapper around merge()/discard()."""
        print(f"\n[Git] Session complete on branch: {self.active_branch}")
        choice = input(f"Merge changes into {self.original_branch}? (y/n): ").strip().lower()
        if choice == "y":
            ok, msg = self.merge()
            print(("[Git] " if ok else "[Git ERROR] ") + msg)
        else:
            ok, msg = self.discard()
            print("[Git] " + msg)