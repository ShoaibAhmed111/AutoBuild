import asyncio
import shutil
import tempfile
from pathlib import Path

from orchestrator import AgentOrchestrator
from verification import TaskStatus, PlanningFailure


def make_project(tmp: str) -> Path:
    root = Path(tmp)
    (root / "app" / "routes").mkdir(parents=True)
    (root / "app" / "static").mkdir(parents=True)
    (root / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from app.routes import orders\n"
        "app = FastAPI()\n"
        "app.include_router(orders.router, prefix='/api')\n"
    )
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "routes" / "__init__.py").write_text("")
    (root / "app" / "routes" / "orders.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/orders')\n"
        "@router.get('/')\n"
        "def list_orders():\n"
        "    return []\n"
    )
    (root / "app" / "static" / "index.html").write_text("<html><body>hi</body></html>")
    return root


class FakeLLM:
    """Replays a scripted sequence of responses, one per call, regardless of
    prompt content (the scripts below encode the scenario intentionally)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, prompt, system_prompt="", max_retries=2):
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self.responses.pop(0)


def run(coro):
    return asyncio.run(coro)


def test_happy_path_fully_verified():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))

        plan_json = """{
          "architectural_summary": "Add a status endpoint",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/status.py", "action_type": "CREATE",
             "impact_level": "LOW", "requirements": "Add GET /status returning ok"}
          ]
        }"""
        new_file_code = (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/status')\n"
            "def status():\n"
            "    return {'ok': True}\n"
        )
        orch.llm = FakeLLM([plan_json, new_file_code])

        report = run(orch.run_task("add a status endpoint", "repo map placeholder"))
        assert report.status == TaskStatus.VERIFICATION_NOT_CONFIGURED, report.status
        assert "app/routes/status.py" in report.files_modified
        assert (root / "app" / "routes" / "status.py").exists()
        print("PASS: happy path with no verification_command -> VERIFICATION_NOT_CONFIGURED (not falsely VERIFIED)")


def test_planning_failure_does_not_guess_a_plan():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))
        # Two garbage responses -> plan() should exhaust its 1 repair attempt and raise
        orch.llm = FakeLLM(["not json at all", "still not json"])

        report = run(orch.run_task("do something", "repo map"))
        assert report.status == TaskStatus.PLANNING_FAILED, report.status
        assert report.files_modified == []
        print("PASS: invalid LLM plan output twice -> PLANNING_FAILED, no guessed fallback plan, no files touched")


def test_static_contract_failure_triggers_real_repair_and_recovers():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))

        plan_json = """{
          "architectural_summary": "Wire frontend to orders",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/static/index.html", "action_type": "MODIFY",
             "impact_level": "LOW", "requirements": "Call the orders API from the frontend"}
          ]
        }"""
        # First write: full-file body containing a HALLUCINATED endpoint (wrong path)
        bad_frontend = "<html><body><script>axios.get('/api/orders/definitely-not-real');</script></body></html>"
        # Repair write: full-file body now calling the REAL endpoint
        good_frontend = "<html><body><script>axios.get('/api/orders');</script></body></html>"

        orch.llm = FakeLLM([plan_json, bad_frontend, good_frontend])

        report = run(orch.run_task("wire frontend to orders", "repo map"))
        assert report.repair_attempts == 1, report.repair_attempts
        assert report.status == TaskStatus.VERIFICATION_NOT_CONFIGURED, report.status
        content = (root / "app" / "static" / "index.html").read_text()
        assert "definitely-not-real" not in content
        assert "/api/orders" in content
        print("PASS: a hallucinated frontend endpoint is caught by static check and genuinely repaired (not just reported)")


def test_ambiguous_patch_requires_approval_not_silent_rewrite():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        # Two structurally-identical helper stubs -- a SEARCH block targeting
        # one generic line ("    return []") genuinely can't tell them apart.
        target = root / "app" / "routes" / "orders.py"
        target.write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/orders')\n"
            "@router.get('/')\n"
            "def list_orders():\n"
            "    return []\n"
            "@router.get('/archived')\n"
            "def list_archived():\n"
            "    return []\n"
        )
        orch = AgentOrchestrator(str(root))

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

        approval_calls = []

        async def on_rewrite_approval(subtask, detail):
            approval_calls.append((subtask.target_file, detail))
            return False  # reject the risky full rewrite

        original_content = target.read_text()
        report = run(orch.run_task(
            "tweak orders", "repo map", on_rewrite_approval=on_rewrite_approval
        ))

        assert len(approval_calls) == 1, approval_calls
        assert "matches 2 identical locations" in approval_calls[0][1], approval_calls
        assert "app/routes/orders.py" not in report.files_modified
        assert report.status == TaskStatus.FAILED, report.status
        # file on disk is untouched -- no silent full rewrite happened
        assert target.read_text() == original_content
        print("PASS: an ambiguous SEARCH match (2 identical locations) escalates to human approval instead of silently picking one; rejection leaves the file untouched")


def test_runtime_failure_exhausts_repair_budget():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))

        plan_json = """{
          "architectural_summary": "Add broken endpoint to existing router",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/orders.py", "action_type": "MODIFY",
             "impact_level": "LOW", "requirements": "Add an endpoint that raises at runtime"}
          ]
        }"""
        # Full-body replacement (no SEARCH markers, large enough to trigger the
        # "full_text" fallback path in apply_diff) of an ALREADY-REGISTERED
        # router, so the new broken route is actually reachable by the probe.
        # Every write (initial + 3 repair attempts) is identically broken --
        # the LLM "can't fix it".
        broken_code = (
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/orders')\n"
            "@router.get('/')\n"
            "def list_orders():\n"
            "    return []\n"
            "@router.get('/broken')\n"
            "def broken():\n"
            "    raise ValueError('always fails')\n"
        )
        orch.llm = FakeLLM([plan_json, broken_code, broken_code, broken_code, broken_code])

        report = run(orch.run_task("add broken endpoint", "repo map"))
        assert report.status == TaskStatus.REPAIR_EXHAUSTED, report.status
        assert report.repair_attempts == 3
        assert any(r.status.value in ("FAILED", "ERROR") and "broken" in r.name for r in report.runtime_results), report.runtime_results
        print("PASS: repeated runtime failure exhausts the bounded repair budget -> REPAIR_EXHAUSTED (not a false success)")


def test_destructive_route_is_skipped_not_silently_passed():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        (root / "app" / "routes" / "orders.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/orders')\n"
            "@router.get('/')\n"
            "def list_orders():\n"
            "    return []\n"
            "@router.delete('/{order_id}')\n"
            "def delete_order(order_id: int):\n"
            "    return {'deleted': order_id}\n"
        )
        orch = AgentOrchestrator(str(root))
        plan_json = """{
          "architectural_summary": "Add unrelated field",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/status.py", "action_type": "CREATE",
             "impact_level": "LOW", "requirements": "Add GET /status"}
          ]
        }"""
        new_code = (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/status')\n"
            "def status():\n"
            "    return {'ok': True}\n"
        )
        orch.llm = FakeLLM([plan_json, new_code])
        report = run(orch.run_task("add status", "repo map"))
        skipped = [r for r in report.runtime_results if r.status.value == "SKIPPED"]
        assert any("DELETE" in r.name for r in skipped), report.runtime_results
        # DELETE was skipped (not executed, not counted as passed) -> the
        # overall verdict must reflect incompleteness, not a clean VERIFIED.
        assert report.status == TaskStatus.PARTIALLY_VERIFIED, report.status
        print("PASS: a skipped destructive route is reported as SKIPPED and downgrades the verdict to PARTIALLY_VERIFIED, never silently counted as passed")


if __name__ == "__main__":
    test_happy_path_fully_verified()
    test_planning_failure_does_not_guess_a_plan()
    test_static_contract_failure_triggers_real_repair_and_recovers()
    test_ambiguous_patch_requires_approval_not_silent_rewrite()
    test_runtime_failure_exhausts_repair_budget()
    test_destructive_route_is_skipped_not_silently_passed()
    print("\nALL ORCHESTRATOR INTEGRATION TESTS PASSED")
