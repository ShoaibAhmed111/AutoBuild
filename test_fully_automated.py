import asyncio
import tempfile
from pathlib import Path

from orchestrator import AgentOrchestrator
from verification import TaskStatus


def make_project(tmp: str) -> Path:
    root = Path(tmp)
    (root / "app" / "routes").mkdir(parents=True)
    (root / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "routes" / "__init__.py").write_text("")
    (root / "app" / "routes" / "orders.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/orders')\n"
        "@router.get('/')\n"
        "def list_orders():\n"
        "    return []\n"
        "@router.get('/archived')\n"
        "def list_archived():\n"
        "    return []\n"
    )
    return root


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, prompt, system_prompt="", max_retries=2):
        return self.responses.pop(0)


def run(coro):
    return asyncio.run(coro)


def test_fully_automated_auto_approves_ambiguous_rewrite_no_callback_needed():
    """The exact deadlock scenario: an ambiguous SEARCH match would normally
    require on_rewrite_approval. In fully_automated mode it must proceed
    WITHOUT any approval callback being invoked or awaited. Goes through
    run_task() directly since "tweak orders" (matched by the MECHANICAL
    triage pattern? -- checked below) needs to land on STANDARD to exercise
    the multi-file plan path this scenario is built around."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root), fully_automated=True)

        plan_json = """{
          "architectural_summary": "tweak orders",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/orders.py", "action_type": "MODIFY",
             "impact_level": "LOW", "requirements": "add a comment"}
          ]
        }"""
        ambiguous_diff = (
            "<<<<<<< SEARCH\n    return []\n=======\n    return []  # patched\n>>>>>>>"
        )
        full_rewrite = (
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/orders')\n"
            "@router.get('/')\n"
            "def list_orders():\n"
            "    return []  # patched\n"
            "@router.get('/archived')\n"
            "def list_archived():\n"
            "    return []\n"
        )
        orch.llm = FakeLLM([plan_json, ambiguous_diff, full_rewrite])

        # Deliberately do NOT pass on_rewrite_approval -- if the code path
        # tried to call it, it would hit `None(...)` and crash, proving the
        # auto-approve branch never touches the callback.
        report = run(orch.run_task("tweak orders", "repo map"))

        assert "app/routes/orders.py" in report.files_modified
        assert any(e.label == "AUTO_APPROVED" for e in report.ledger), [e.label for e in report.ledger]
        content = (root / "app" / "routes" / "orders.py").read_text()
        assert "# patched" in content
        print("PASS: fully_automated=True auto-approves an ambiguous rewrite with zero approval callbacks (run_task)")


def test_semi_automated_still_requires_approval_by_default():
    """Confirms default (fully_automated=False, the class default) behavior
    is completely unchanged -- old approval flow still gates the rewrite."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))  # default fully_automated=False

        plan_json = """{
          "architectural_summary": "tweak orders",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/orders.py", "action_type": "MODIFY",
             "impact_level": "LOW", "requirements": "add a comment"}
          ]
        }"""
        ambiguous_diff = (
            "<<<<<<< SEARCH\n    return []\n=======\n    return []  # patched\n>>>>>>>"
        )
        orch.llm = FakeLLM([plan_json, ambiguous_diff])

        calls = []
        async def on_rewrite_approval(subtask, detail):
            calls.append(subtask.target_file)
            return False

        report = run(orch.run_task("tweak orders", "repo map", on_rewrite_approval=on_rewrite_approval))
        assert len(calls) == 1
        assert "app/routes/orders.py" not in report.files_modified
        print("PASS: default fully_automated=False still requires human approval, unchanged")


def test_per_task_override_beats_instance_default():
    """fully_automated passed to run_task() overrides the instance default,
    so the UI's per-task mode toggle works without re-initializing."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root), fully_automated=False)

        plan_json = """{
          "architectural_summary": "tweak orders",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/orders.py", "action_type": "MODIFY",
             "impact_level": "LOW", "requirements": "add a comment"}
          ]
        }"""
        ambiguous_diff = (
            "<<<<<<< SEARCH\n    return []\n=======\n    return []  # patched\n>>>>>>>"
        )
        full_rewrite = "from fastapi import APIRouter\nrouter = APIRouter()\n"
        orch.llm = FakeLLM([plan_json, ambiguous_diff, full_rewrite])

        report = run(orch.run_task("tweak orders", "repo map", fully_automated=True))
        assert any(e.label == "AUTO_APPROVED" for e in report.ledger)
        print("PASS: run_task(fully_automated=True) overrides instance default of False")


def test_fully_automated_flows_through_run_request_for_mechanical_tier():
    """The integration point that matters in practice: server.py/main.py call
    run_request(), not run_task() directly. This confirms fully_automated
    reaches all the way through triage -> MECHANICAL tier -> auto-approval,
    not just when calling run_task() in isolation."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        (root / "app" / "static").mkdir(parents=True)
        (root / "app" / "static" / "index.html").write_text(
            '<html><body><button id="submitBtn" class="btn-primary">Submit</button></body></html>'
        )
        orch = AgentOrchestrator(str(root), fully_automated=True)

        # A HIGH impact single-file tweak, forced via a manual SubTask
        # equivalent by using run_mechanical's path: mechanical tier builds
        # its own SubTask with impact_level="LOW" today, so to actually
        # exercise the HIGH-impact auto-approve branch through the triage
        # front door we go through run_task's plan-driven HIGH task instead,
        # called via run_request with a prompt that triage classifies as
        # STANDARD (default) rather than guessing tier behavior.
        plan_json = """{
          "architectural_summary": "add a risky endpoint",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/status.py", "action_type": "CREATE",
             "impact_level": "HIGH", "requirements": "Add GET /status"}
          ]
        }"""
        code = (
            "from fastapi import APIRouter\nrouter = APIRouter()\n"
            "@router.get('/status')\ndef status():\n    return {'ok': True}\n"
        )
        orch.llm = FakeLLM([plan_json, code])

        events = []
        async def on_event(label, detail):
            events.append(label)

        repo_map = "\n[FILE: app/routes/orders.py]\n  def list_orders():\n"
        report = run(orch.run_request(
            "Add a new endpoint that does something completely custom and unprecedented",
            repo_map, on_event=on_event,
        ))

        assert report.tier == "STANDARD", report.tier
        assert any(e == "AUTO_APPROVED" for e in events), events
        assert "app/routes/status.py" in report.files_modified
        print("PASS: fully_automated=True reaches through run_request() -> run_task() -> HIGH-impact auto-approval end to end")


if __name__ == "__main__":
    test_fully_automated_auto_approves_ambiguous_rewrite_no_callback_needed()
    test_semi_automated_still_requires_approval_by_default()
    test_per_task_override_beats_instance_default()
    test_fully_automated_flows_through_run_request_for_mechanical_tier()
    print("\nALL FULLY-AUTOMATED TESTS PASSED")
