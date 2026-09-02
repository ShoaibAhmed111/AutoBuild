import asyncio
import tempfile
from pathlib import Path

from orchestrator import AgentOrchestrator
from verification import TaskStatus


def make_project(tmp: str) -> Path:
    root = Path(tmp)
    (root / "app" / "routes").mkdir(parents=True)
    (root / "app" / "static").mkdir(parents=True)
    (root / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "static" / "index.html").write_text(
        '<html><body><button id="submitBtn" class="btn-primary">Submit</button></body></html>'
    )
    return root


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    def generate(self, prompt, system_prompt="", max_retries=2):
        self.call_count += 1
        if not self.responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self.responses.pop(0)


def run(coro):
    return asyncio.run(coro)


def test_mechanical_tier_skips_planning_and_uses_one_write_call():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))

        # Only ONE scripted response: a direct file edit. If this test
        # required a planning call too, FakeLLM would run out of responses
        # and raise -- so passing at all proves planning was skipped.
        patch = (
            "<<<<<<< SEARCH\n"
            '<button id="submitBtn" class="btn-primary">Submit</button>\n'
            "=======\n"
            '<button id="submitBtn" class="btn-primary" style="background:blue">Submit</button>\n'
            ">>>>>>>"
        )
        orch.llm = FakeLLM([patch])

        events = []
        async def on_event(label, detail):
            events.append(label)

        repo_map = "\n[FILE: app/static/index.html]\n  <button id=\"submitBtn\" class=\"btn-primary\">Submit</button>\n"
        report = run(orch.run_request("change the submit button color to blue", repo_map, on_event=on_event))

        assert orch.llm.call_count == 1, f"expected exactly 1 LLM call (no planning), got {orch.llm.call_count}"
        assert report.tier == "MECHANICAL", report.tier
        assert "app/static/index.html" in report.files_modified
        assert "background:blue" in (root / "app" / "static" / "index.html").read_text()
        assert "TRIAGE_DECISION" in events
        assert "MECHANICAL_FILE_SELECTED" in events
        print("PASS: a short, single-concern UI tweak skips multi-file planning entirely and uses exactly 1 LLM call")


def test_guidance_only_tier_makes_no_file_writes():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))

        guidance_json = """{
          "summary": "This requires a coordinated schema migration across multiple services.",
          "affected_files": [
            {"file": "app/routes/orders.py", "why": "Defines the current schema", "what_to_change": "Add new column with a default, deploy, then backfill"}
          ],
          "steps": ["Write a migration script", "Deploy with the old and new column present", "Backfill data", "Remove the old column in a follow-up release"]
        }"""
        orch.llm = FakeLLM([guidance_json])

        before_snapshot = {f: (root / f).read_text() for f in ["app/main.py", "app/static/index.html"]}

        report = run(orch.run_request(
            "migrate the database schema to add a new orders table", "repo map placeholder"
        ))

        assert orch.llm.call_count == 1
        assert report.tier == "GUIDANCE_ONLY"
        assert report.status == TaskStatus.GUIDANCE_PROVIDED
        assert report.files_modified == [], "guidance mode must never write files"
        for f, content in before_snapshot.items():
            assert (root / f).read_text() == content, f"{f} was modified but guidance mode should never touch disk"
        assert "orders.py" in report.guidance.affected_files[0].file
        assert len(report.guidance.steps) == 4
        print("PASS: a high-risk request produces structured guidance with zero file writes, verified against disk snapshots")


def test_standard_tier_still_used_for_multi_file_feature_work():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))

        plan_json = """{
          "architectural_summary": "Add a status endpoint",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/routes/status.py", "action_type": "CREATE",
             "impact_level": "LOW", "requirements": "Add GET /status"}
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

        report = run(orch.run_request("Add an AI concierge endpoint and connect it to the frontend", "repo map", on_event=on_event))
        assert report.tier == "STANDARD"
        assert "TRIAGE_DECISION" in events
        assert "app/routes/status.py" in report.files_modified
        print("PASS: an ordinary multi-step feature request still goes through the full plan -> write -> verify pipeline")


def test_mechanical_falls_back_to_standard_when_no_file_can_be_picked():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        orch = AgentOrchestrator(str(root))

        # repo_map shares NO keywords with the prompt -> pick_mechanical_target_file
        # returns None -> must fall back to full planning rather than guessing.
        plan_json = """{
          "architectural_summary": "fallback plan",
          "verification_command": "none",
          "tasks": [
            {"step_number": 1, "target_file": "app/static/index.html", "action_type": "MODIFY",
             "impact_level": "LOW", "requirements": "tweak style"}
          ]
        }"""
        rewrite = '<html><body>updated</body></html>'
        orch.llm = FakeLLM([plan_json, rewrite])

        events = []
        async def on_event(label, detail):
            events.append(label)

        report = run(orch.run_request("tweak the style of something totally unrelated zzzqqqxyz", "no matching keywords here", on_event=on_event))
        assert "MECHANICAL_FALLBACK" in events
        assert report.tier == "STANDARD"
        print("PASS: when the mechanical file-picker can't confidently identify a target, it falls back to full planning instead of guessing")


if __name__ == "__main__":
    test_mechanical_tier_skips_planning_and_uses_one_write_call()
    test_guidance_only_tier_makes_no_file_writes()
    test_standard_tier_still_used_for_multi_file_feature_work()
    test_mechanical_falls_back_to_standard_when_no_file_can_be_picked()
    print("\nALL TRIAGE/RUN_REQUEST TESTS PASSED")
