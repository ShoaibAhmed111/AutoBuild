import asyncio
import sys
from pathlib import Path

from git_sandbox import GitSandbox
from indexer import RepoIndexer
from llm_client import UnknownModelError
from orchestrator import AgentOrchestrator
from verification import TaskStatus


async def on_event(label: str, detail: str):
    if label == "PLAN_GENERATED":
        print(f"\n[Plan] {detail}")
    elif label == "SUBTASK_EXECUTED":
        print(f"[Worker] {detail}")
    elif label.startswith("REPAIR_ATTEMPT_"):
        print(f"[Self-Healing] {label}: {detail}")
    elif label in ("STATIC_CHECK_FAILED", "RUNTIME_CHECK_FAILED", "COMMAND_CHECK_FAILED", "SUBTASK_ERROR", "PLANNING_FAILED"):
        print(f"[FAILED] {label}: {detail}")
    elif label in ("STATIC_CHECK_PASSED", "RUNTIME_CHECK_PASSED", "COMMAND_CHECK_PASSED"):
        print(f"[OK] {label}: {detail}")
    elif label == "TASK_FINISHED":
        pass  # final summary printed separately below
    else:
        print(f"[{label}] {detail}")


async def on_high_impact_approval(subtask) -> bool:
    choice = input(
        f"\n[Approval Required] HIGH impact change -> {subtask.target_file}\n"
        f"  {subtask.requirements}\nApprove? (y/n): "
    ).strip().lower()
    return choice == "y"


async def on_rewrite_approval(subtask, detail) -> bool:
    choice = input(
        f"\n[Approval Required] Surgical patch failed for {subtask.target_file}.\n"
        f"  Reason: {detail}\n"
        f"  A full-file rewrite would be required, which risks discarding existing content.\n"
        f"Allow full rewrite? (y/n): "
    ).strip().lower()
    return choice == "y"


async def run(project_path: str, prompt: str):
    print(f"[Init] Starting Auto Build CLI on {project_path}")

    sandbox = GitSandbox(project_path)
    sandbox.create_session_branch()

    try:
        indexer = RepoIndexer(project_path)
        repo_map = indexer.generate_repo_map()

        orchestrator = AgentOrchestrator(project_path)
        try:
            orchestrator.llm.set_active_model("ollama_14b")  # default to local
        except UnknownModelError as e:
            print(f"[Fatal Error] {e}")
            return

        report = await orchestrator.run_task(
            prompt,
            repo_map,
            on_event=on_event,
            on_high_impact_approval=on_high_impact_approval,
            on_rewrite_approval=on_rewrite_approval,
        )

        if report.files_modified:
            commit_ok, commit_msg = sandbox.commit_changes(f"Auto Build: {prompt[:72]}")
            if not commit_ok:
                print(f"[Git ERROR] {commit_msg}")
            diff_text = sandbox.diff_against_original()
            if diff_text:
                print(f"\n[Diff]\n{diff_text[:4000]}")

        print(f"\n[Result] {report.status.value}")
        print(report.evidence_text())

        if report.status == TaskStatus.PLANNING_FAILED:
            print("\n[Note] No files were modified -- planning failed and no guessed fallback plan was executed.")
        elif report.status == TaskStatus.VERIFIED:
            print("\n[Success] Task completed and verified across static, runtime, and command checks.")
        elif report.status == TaskStatus.VERIFICATION_NOT_CONFIGURED:
            print("\n[Warning] Files were written and passed the checks that ran, but verification_command was "
                  "'none' -- there is no automated confirmation the feature actually works end to end.")
        elif report.status == TaskStatus.PARTIALLY_VERIFIED:
            print("\n[Warning] Some checks were skipped/unresolved rather than passed. This is NOT a full pass -- "
                  "review report.evidence_text() above for exactly what was and wasn't checked.")
        elif report.status == TaskStatus.REPAIR_EXHAUSTED:
            print(f"\n[FAILED] Verification kept failing after {report.repair_attempts} repair attempts. "
                  f"The changes are on branch '{sandbox.active_branch}' for manual review -- do not assume they work.")
        else:
            print("\n[FAILED] Task did not pass verification. Review the evidence above before merging.")

    except Exception as e:
        print(f"\n[Fatal Error] {e}")
    finally:
        sandbox.finalize()


def main():
    if len(sys.argv) < 3:
        print("Usage: python main.py <project_path> <prompt>")
        sys.exit(1)

    project_path = str(Path(sys.argv[1]).resolve())
    prompt = " ".join(sys.argv[2:])
    asyncio.run(run(project_path, prompt))


if __name__ == "__main__":
    main()
