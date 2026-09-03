import json
import os
import subprocess
import tempfile
from pathlib import Path

# Must be set BEFORE importing server -- see test_server.py for why.
os.environ["AUTOBUILD_AUTH_TOKEN"] = "test-token-for-suite"

from fastapi.testclient import TestClient

import server
from orchestrator import AgentOrchestrator

AUTH_TOKEN = "test-token-for-suite"


def sh(cmd, cwd):
    subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, prompt, system_prompt="", max_retries=2):
        return self.responses.pop(0)

    def set_active_model(self, key):
        from llm_client import MODEL_PRESETS, UnknownModelError
        if key not in MODEL_PRESETS:
            raise UnknownModelError(f"Unknown model key '{key}'")
        self.active_key = key

    def is_local(self):
        return getattr(self, "active_key", "") in ("ollama_14b", "ollama_7b")


def make_project(tmp):
    root = Path(tmp)
    (root / "app" / "routes").mkdir(parents=True)
    (root / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "routes" / "__init__.py").write_text("")
    sh("git init -q", root)
    sh("git config user.email t@t.com", root)
    sh("git config user.name T", root)
    sh("git add . && git commit -q -m init", root)
    return root


def test_task_payload_fully_automated_skips_approval_over_websocket():
    """An ambiguous SEARCH match would normally emit approval_request and
    block. With fully_automated: true in the task payload, the task must
    reach a terminal status WITHOUT any approval_request ever being sent."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
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
        sh("git add . && git commit -q -m orders", root)

        plan_json = json.dumps({
            "architectural_summary": "tweak orders",
            "verification_command": "none",
            "tasks": [
                {"step_number": 1, "target_file": "app/routes/orders.py", "action_type": "MODIFY",
                 "impact_level": "LOW", "requirements": "add a comment"}
            ],
        })
        ambiguous_diff = "<<<<<<< SEARCH\n    return []\n=======\n    return []  # patched\n>>>>>>>"
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

        original_init = AgentOrchestrator.__init__

        def patched_init(self, project_dir):
            original_init(self, project_dir)
            self.llm = FakeLLM([plan_json, ambiguous_diff, full_rewrite])

        AgentOrchestrator.__init__ = patched_init
        try:
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_14b", "auth_token": AUTH_TOKEN}))
                for _ in range(10):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "log" and "Ready" in msg.get("text", ""):
                        break

                # A prompt carefully chosen to land on STANDARD tier: it must
                # not match any of triage.py's own MECHANICAL_PATTERNS (e.g.
                # "tweak", "comment", "style" all route elsewhere now that
                # triage exists) or HIGH_RISK_PATTERNS, since this scenario is
                # built around exercising the multi-file plan path.
                ws.send_text(json.dumps({
                    "action": "task",
                    "prompt": "Improve the orders router's response handling logic for clarity",
                    "target_dir": str(root),
                    "fully_automated": True,
                }))

                saw_approval_request = False
                saw_auto_approved_log = False
                saw_decision_needed = False
                for _ in range(60):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "approval_request":
                        saw_approval_request = True
                    if msg.get("type") == "log" and "AUTO_APPROVED" in msg.get("text", ""):
                        saw_auto_approved_log = True
                    if msg.get("type") == "decision_needed":
                        saw_decision_needed = True
                        break

                assert not saw_approval_request, "fully_automated task must never trigger approval_request"
                assert saw_auto_approved_log, "expected an [AUTO_APPROVED] log line"
                assert saw_decision_needed
                content = (root / "app" / "routes" / "orders.py").read_text()
                assert "# patched" in content
                print("PASS: task payload's fully_automated:true reaches the orchestrator and skips approval end-to-end")
        finally:
            AgentOrchestrator.__init__ = original_init


def test_task_target_dir_mismatch_is_logged_not_silently_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        plan_json = json.dumps({
            "architectural_summary": "noop",
            "verification_command": "none",
            "tasks": [],
        })
        original_init = AgentOrchestrator.__init__

        def patched_init(self, project_dir):
            original_init(self, project_dir)
            self.llm = FakeLLM([plan_json])

        AgentOrchestrator.__init__ = patched_init
        try:
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_14b", "auth_token": AUTH_TOKEN}))
                for _ in range(10):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "log" and "Ready" in msg.get("text", ""):
                        break

                ws.send_text(json.dumps({
                    "action": "task",
                    "prompt": "do a totally unrelated abstract refactor of everything",
                    "target_dir": "/some/other/path",
                    "fully_automated": False,
                }))
                saw_mismatch_warning = False
                for _ in range(20):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "log" and "differs from the initialized project root" in msg.get("text", ""):
                        saw_mismatch_warning = True
                        break
                assert saw_mismatch_warning
                print("PASS: a target_dir mismatch in the task payload is surfaced as a warning, not silently dropped")
        finally:
            AgentOrchestrator.__init__ = original_init


if __name__ == "__main__":
    test_task_payload_fully_automated_skips_approval_over_websocket()
    test_task_target_dir_mismatch_is_logged_not_silently_ignored()
    print("\nALL SERVER FULLY-AUTOMATED TESTS PASSED")
