import json
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import server
from orchestrator import AgentOrchestrator


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
                ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_14b"}))
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
            ws.send_text(json.dumps({"action": "init", "path": str(root), "model": "ollama_local"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "log" and "Unknown model key" in msg["text"], msg
            print("PASS: an invalid/mistyped model key is rejected with a visible error instead of silently routing to a cloud provider")


if __name__ == "__main__":
    test_full_websocket_flow_reports_real_status_and_merges()
    test_unknown_model_key_is_rejected_not_silently_swapped()
    print("\nALL SERVER TESTS PASSED")
