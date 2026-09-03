import json
import os
import subprocess
import tempfile
from pathlib import Path

# Must be set BEFORE importing server, since server.py reads
# AUTOBUILD_AUTH_TOKEN once at import time to decide between a fixed token
# and a randomly generated one.
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


def test_full_websocket_flow_reports_real_status_and_merges():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)

        plan_json = json.dumps({
            "architectural_summary": "add a status endpoint",
            "verification_command": "none",
            "tasks": [
                {"step_number": 1, "target_file": "app/routes/status.py", "action_type": "CREATE",
                 "impact_level": "LOW", "requirements": "Add GET /status"}
            ],
        })
        code = (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/status')\n"
            "def status():\n"
            "    return {'ok': True}\n"
        )

        original_init = AgentOrchestrator.__init__

        def patched_init(self, project_dir):
            original_init(self, project_dir)
            self.llm = FakeLLM([plan_json, code])

        AgentOrchestrator.__init__ = patched_init
        try:
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_14b", "auth_token": AUTH_TOKEN}))
                messages = []
                # drain messages until we see the "Ready" log
                for _ in range(10):
                    msg = json.loads(ws.receive_text())
                    messages.append(msg)
                    if msg.get("type") == "log" and "Ready" in msg.get("text", ""):
                        break
                ready_msg = next(m for m in messages if "Ready" in m.get("text", ""))
                assert "LOCAL" in ready_msg["text"], ready_msg
                print("PASS: init reports LOCAL vs CLOUD explicitly")

                ws.send_text(json.dumps({"action": "task", "prompt": "add a status endpoint"}))

                saw_decision_needed = False
                saw_status_partial_or_success = None
                for _ in range(60):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "status" and msg.get("state") in ("success", "partial", "failed"):
                        saw_status_partial_or_success = msg
                    if msg.get("type") == "decision_needed":
                        saw_decision_needed = True
                        break

                assert saw_status_partial_or_success is not None
                # VERIFICATION_NOT_CONFIGURED maps to "partial", never "success",
                # since verification_command was "none" -- this is the direct
                # fix for the old `if success:` truthy-object bug, which used
                # to print "Task completed and verified successfully" no
                # matter what the VerificationResult actually contained.
                assert saw_status_partial_or_success["state"] == "partial", saw_status_partial_or_success
                assert saw_decision_needed
                print("PASS: server reports the real structured status (partial, not a blanket 'success') and asks for a merge/discard decision")

                assert (root / "app" / "routes" / "status.py").exists()

                ws.send_text(json.dumps({"action": "merge"}))
                merge_log = None
                for _ in range(10):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "log" and "merged" in msg.get("text", "").lower():
                        merge_log = msg
                        break
                assert merge_log is not None
                print("PASS: web UI now has a working merge action (old code had NO merge/discard path at all)")
        finally:
            AgentOrchestrator.__init__ = original_init


def test_unknown_model_key_is_rejected_not_silently_swapped():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws:
            # the OLD default was "ollama_local", which isn't a real preset key
            ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_local", "auth_token": AUTH_TOKEN}))
            saw_unknown_model_error = False
            for _ in range(5):
                msg = json.loads(ws.receive_text())
                if msg.get("type") == "log" and "Unknown model key" in msg.get("text", ""):
                    saw_unknown_model_error = True
                    break
            assert saw_unknown_model_error
            print("PASS: an invalid/mistyped model key is rejected with a visible error instead of silently routing to a cloud provider")


def test_guidance_request_over_websocket_writes_no_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)

        guidance_json = json.dumps({
            "summary": "This requires a careful, multi-step database schema migration.",
            "affected_files": [{"file": "app/routes/status.py", "why": "reads the current schema", "what_to_change": "add new column with a safe default first"}],
            "steps": ["Write migration", "Deploy dual-write", "Backfill", "Cut over"],
        })

        original_init = AgentOrchestrator.__init__

        def patched_init(self, project_dir):
            original_init(self, project_dir)
            self.llm = FakeLLM([guidance_json])

        AgentOrchestrator.__init__ = patched_init
        try:
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_14b", "auth_token": AUTH_TOKEN}))
                for _ in range(10):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "log" and "Ready" in msg.get("text", ""):
                        break

                ws.send_text(json.dumps({"action": "task", "prompt": "migrate the database schema to add a new column"}))

                saw_guidance = None
                saw_status = None
                for _ in range(30):
                    msg = json.loads(ws.receive_text())
                    if msg.get("type") == "guidance":
                        saw_guidance = msg
                    if msg.get("type") == "status" and msg.get("state") == "guidance":
                        saw_status = msg
                    if saw_guidance and saw_status:
                        break

                assert saw_guidance is not None, "expected a 'guidance' message over the websocket"
                assert "migration" in saw_guidance["text"].lower()
                assert saw_status is not None
                assert not (root / "app" / "routes" / "status.py").exists(), "guidance mode must never create/modify files"
                print("PASS: a high-risk request over the websocket produces a guidance panel and touches no files")
        finally:
            AgentOrchestrator.__init__ = original_init


def test_auth_gate_rejects_missing_or_wrong_token():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        client = TestClient(server.app)

        # No token at all on an init action -> falls through to the token
        # comparison (None != real token) and gets the specific message.
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_14b"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "log" and "Invalid or missing auth token" in msg["text"], msg

        # Wrong token
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_14b", "auth_token": "wrong-token"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "log" and "Invalid or missing auth token" in msg["text"], msg

        # A "task" action before authentication must also be refused, not
        # silently accepted.
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"action": "task", "prompt": "do anything"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "log" and "Not authenticated" in msg["text"], msg

        print("PASS: the auth gate rejects a missing token, a wrong token, and any non-init action before authentication")


if __name__ == "__main__":
    test_full_websocket_flow_reports_real_status_and_merges()
    test_unknown_model_key_is_rejected_not_silently_swapped()
    test_guidance_request_over_websocket_writes_no_files()
    test_auth_gate_rejects_missing_or_wrong_token()
    print("\nALL SERVER TESTS PASSED")
