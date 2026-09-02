# server.py
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from git_sandbox import GitSandbox
from indexer import RepoIndexer
from llm_client import UnknownModelError
from orchestrator import AgentOrchestrator, SubTask
from verification import TaskStatus

app = FastAPI()

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Auto Build - Autonomous Dev Agent</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; }
        body { background: #0f172a; color: #f8fafc; display: flex; height: 100vh; overflow: hidden; }

        #sidebar { width: 320px; background: #1e293b; border-right: 1px solid #334155; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
        .brand-title { font-size: 20px; font-weight: bold; color: #38bdf8; display: flex; align-items: center; gap: 8px; }

        #status-card { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
        .status-header { display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: #94a3b8; }
        .badge { font-weight: bold; font-size: 11px; padding: 3px 8px; border-radius: 9999px; text-transform: uppercase; }
        .badge-idle { background: #334155; color: #94a3b8; }
        .badge-processing { background: #854d0e; color: #facc15; animation: pulse 1.5s infinite; }
        .badge-success { background: #14532d; color: #4ade80; }
        .badge-partial { background: #7c4a03; color: #fbbf24; }
        .badge-guidance { background: #1e3a5f; color: #7dd3fc; }
        .badge-failed { background: #7f1d1d; color: #f87171; }

        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

        #progress-bar-container { width: 100%; height: 6px; background: #334155; border-radius: 4px; overflow: hidden; display: none; }
        #progress-bar { width: 0%; height: 100%; background: #38bdf8; transition: width 0.3s ease; }

        #main { flex: 1; display: flex; flex-direction: column; position: relative; }
        #logs { flex: 1; background: #020617; padding: 20px; overflow-y: auto; font-size: 13px; line-height: 1.6; border-bottom: 1px solid #334155; }
        #diff-container { height: 190px; background: #0b0f19; padding: 15px; overflow-y: auto; font-size: 12px; color: #38bdf8; white-space: pre-wrap; font-family: monospace; }
        #decision-panel { padding: 10px 20px; background: #1e293b; display: none; gap: 10px; align-items: center; border-top: 1px solid #334155; }
        #input-panel { padding: 15px 20px; background: #1e293b; display: flex; gap: 10px; }

        input, select, textarea, button { border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #fff; padding: 10px; font-size: 14px; }
        input, select { width: 100%; }
        textarea { flex: 1; resize: none; height: 50px; }
        button { background: #2563eb; color: #fff; font-weight: bold; cursor: pointer; border: none; padding: 0 20px; transition: background 0.2s; }
        button:hover { background: #1d4ed8; }
        button:disabled { background: #475569; cursor: not-allowed; }

        .log-entry { margin-bottom: 4px; }
        .log-worker { color: #facc15; }
        .log-success { color: #4ade80; font-weight: 500; }
        .log-error { color: #f87171; font-weight: 500; }
        .log-plan { color: #a78bfa; font-weight: bold; }

        #approval-box {
            display: none; position: absolute; top: 20px; right: 20px; width: 420px;
            background: #1e293b; border: 2px solid #eab308; border-radius: 8px; padding: 20px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5); z-index: 100;
        }
        #approval-box h3 { color: #eab308; margin-bottom: 10px; font-size: 15px; }
        #approval-desc { font-size: 13px; color: #cbd5e1; margin-bottom: 15px; line-height: 1.4; white-space: pre-wrap; }
        .approval-actions { display: flex; gap: 10px; justify-content: flex-end; }
        .btn-approve { background: #16a34a !important; }
        .btn-reject { background: #dc2626 !important; }
        .btn-merge { background: #16a34a !important; }
        .btn-discard { background: #dc2626 !important; }
    </style>
</head>
<body>
    <div id="sidebar">
        <div class="brand-title">
            <span>&#9889;</span> Auto Build
        </div>

        <div id="status-card">
            <div class="status-header">
                <span>Task State</span>
                <span id="status-badge" class="badge badge-idle">IDLE</span>
            </div>
            <div id="status-message" style="font-size: 12px; color: #cbd5e1;">Ready for instructions.</div>
            <div id="progress-bar-container">
                <div id="progress-bar"></div>
            </div>
        </div>

        <div>
            <label style="font-size: 12px; color: #94a3b8; display: block; margin-bottom: 6px;">Active LLM Brain</label>
           <select id="modelSelector">
               <option value="ollama_14b" selected>Local Ollama: Qwen 2.5 Coder 14B (Recommended)</option>
                <option value="ollama_7b">Local Ollama: Qwen 2.5 Coder 7B</option>
                <option value="gemini_flash">Google Gemini 2.5 Flash (cloud)</option>
                <option value="openrouter_qwen">OpenRouter: Qwen 2.5 Coder 32B (cloud)</option>
                <option value="openrouter_r1">OpenRouter: DeepSeek R1 (cloud)</option>
             </select>
        </div>

        <div>
            <label style="font-size: 12px; color: #94a3b8; display: block; margin-bottom: 6px;">Target Project Path</label>
            <input type="text" id="projectPath" placeholder="/absolute/path/to/your/project" />
        </div>
        <button onclick="initSession()">Initialize / Reset</button>
    </div>

    <div id="main">
        <div id="approval-box">
            <h3 id="approval-title">Approval Required</h3>
            <p id="approval-desc"></p>
            <div class="approval-actions">
                <button class="btn-reject" onclick="respondApproval(false)">Reject / Skip</button>
                <button class="btn-approve" onclick="respondApproval(true)">Approve</button>
            </div>
        </div>

        <div id="logs"></div>
        <div id="guidance-panel" style="display:none; background:#0b1e33; border-top:2px solid #38bdf8; padding:15px 20px; max-height:220px; overflow-y:auto; font-size:13px; white-space:pre-wrap; color:#bae6fd;"></div>
        <div id="diff-container">// Git diff will appear here after task execution...</div>
        <div id="decision-panel">
            <span style="flex:1; font-size:13px; color:#cbd5e1;">Task finished. Review the diff above, then decide:</span>
            <button class="btn-merge" onclick="decide('merge')">Merge into main</button>
            <button class="btn-discard" onclick="decide('discard')">Discard changes</button>
        </div>
        <div id="input-panel">
            <textarea id="promptInput" placeholder="Describe what you want to build or modify..."></textarea>
            <button id="execBtn" onclick="sendTask()">Execute</button>
        </div>
    </div>

    <script>
        let ws;
        const statusBadge = document.getElementById("status-badge");
        const statusMsg = document.getElementById("status-message");
        const progressBarContainer = document.getElementById("progress-bar-container");
        const progressBar = document.getElementById("progress-bar");
        const execBtn = document.getElementById("execBtn");
        const decisionPanel = document.getElementById("decision-panel");

        function setStatus(state, message, progress) {
            const cls = {success:"success", partial:"partial", failed:"failed", processing:"processing", idle:"idle", guidance:"guidance"}[state] || "idle";
            statusBadge.className = "badge badge-" + cls;
            statusBadge.innerText = state.toUpperCase();
            statusMsg.innerText = message;

            if (state === "processing") {
                progressBarContainer.style.display = "block";
                execBtn.disabled = true;
                if (progress !== null && progress !== undefined) progressBar.style.width = progress + "%";
            } else {
                progressBarContainer.style.display = "none";
                progressBar.style.width = "0%";
                execBtn.disabled = false;
            }
        }

        function connect() {
            ws = new WebSocket("ws://" + window.location.host + "/ws");
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);

                if (data.type === "status") {
                    setStatus(data.state, data.message, data.progress);
                } else if (data.type === "log") {
                    const logs = document.getElementById("logs");
                    const div = document.createElement("div");
                    div.className = "log-entry " + (data.style || "");
                    div.innerText = data.text;
                    logs.appendChild(div);
                    logs.scrollTop = logs.scrollHeight;
                } else if (data.type === "diff") {
                    document.getElementById("diff-container").innerText = data.text || "// No pending changes.";
                } else if (data.type === "guidance") {
                    const panel = document.getElementById("guidance-panel");
                    panel.style.display = "block";
                    panel.innerText = data.text;
                } else if (data.type === "decision_needed") {
                    decisionPanel.style.display = "flex";
                } else if (data.type === "approval_request") {
                    document.getElementById("approval-title").innerText =
                        data.kind === "rewrite" ? "Full Rewrite Approval Required" : "Approval Required";
                    document.getElementById("approval-desc").innerText =
                        `[Step ${data.step || ""}] ${data.action || ""} -> ${data.file}\\n\\n${data.req}`;
                    document.getElementById("approval-box").style.display = "block";
                }
            };
            ws.onclose = () => setTimeout(connect, 1500);
        }

        function respondApproval(approved) {
            document.getElementById("approval-box").style.display = "none";
            ws.send(JSON.stringify({ action: "approval_response", approved: approved }));
        }

        function decide(choice) {
            decisionPanel.style.display = "none";
            ws.send(JSON.stringify({ action: choice }));
        }

        function initSession() {
            const path = document.getElementById("projectPath").value;
            const model = document.getElementById("modelSelector").value;
            ws.send(JSON.stringify({ action: "init", path: path, model: model }));
        }

        function sendTask() {
            const prompt = document.getElementById("promptInput").value;
            if (!prompt.trim()) return;
            decisionPanel.style.display = "none";
            document.getElementById("guidance-panel").style.display = "none";
            ws.send(JSON.stringify({ action: "task", prompt: prompt }));
            document.getElementById("promptInput").value = "";
        }

        window.onload = connect;
    </script>
</body>
</html>
"""


@app.get("/")
def get_ui():
    return HTMLResponse(HTML_CONTENT)


# Basic, explicit status->badge mapping so the UI never implies a stronger
# guarantee than the pipeline actually produced.
_STATUS_TO_BADGE = {
    TaskStatus.VERIFIED: ("success", "Task completed and verified (static + runtime + command)."),
    TaskStatus.VERIFICATION_NOT_CONFIGURED: ("partial", "Files written and checks that ran passed, but no verification_command was configured -- not independently confirmed working."),
    TaskStatus.PARTIALLY_VERIFIED: ("partial", "Some checks were skipped or unresolved rather than passed -- review the evidence, this is not a full pass."),
    TaskStatus.FAILED: ("failed", "Task finished but verification failed."),
    TaskStatus.REPAIR_EXHAUSTED: ("failed", "Verification kept failing after the repair budget was exhausted."),
    TaskStatus.PLANNING_FAILED: ("failed", "The LLM could not produce a valid plan; no files were changed."),
    TaskStatus.GUIDANCE_PROVIDED: ("guidance", "This was flagged too risky/cross-cutting to auto-apply -- instructions provided instead, no files were changed."),
}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    orchestrator: AgentOrchestrator | None = None
    indexer: RepoIndexer | None = None
    sandbox: GitSandbox | None = None
    approval_future: asyncio.Future | None = None
    task_running = False
    task_finished_pending_decision = False

    async def log(text: str, style: str = ""):
        try:
            await websocket.send_text(json.dumps({"type": "log", "text": text, "style": style}))
        except Exception:
            pass

    async def update_status(state: str, message: str, progress=None):
        try:
            await websocket.send_text(json.dumps({"type": "status", "state": state, "message": message, "progress": progress}))
        except Exception:
            pass

    async def on_event(label: str, detail: str):
        style_map = {
            "PLAN_GENERATED": "log-plan",
            "SUBTASK_EXECUTED": "log-worker",
            "STATIC_CHECK_FAILED": "log-error",
            "RUNTIME_CHECK_FAILED": "log-error",
            "COMMAND_CHECK_FAILED": "log-error",
            "SUBTASK_ERROR": "log-error",
            "PLANNING_FAILED": "log-error",
            "STATIC_CHECK_PASSED": "log-success",
            "RUNTIME_CHECK_PASSED": "log-success",
            "COMMAND_CHECK_PASSED": "log-success",
        }
        await log(f"[{label}] {detail}", style_map.get(label, ""))

    async def on_high_impact_approval(subtask: SubTask) -> bool:
        nonlocal approval_future
        await websocket.send_text(json.dumps({
            "type": "approval_request", "kind": "high_impact",
            "step": subtask.step_number, "action": subtask.action_type,
            "file": subtask.target_file, "req": subtask.requirements,
        }))
        await log(f"[Waiting] Approval required for: {subtask.target_file}...", "log-worker")
        loop = asyncio.get_running_loop()
        approval_future = loop.create_future()
        return await approval_future

    async def on_rewrite_approval(subtask: SubTask, detail: str) -> bool:
        nonlocal approval_future
        await websocket.send_text(json.dumps({
            "type": "approval_request", "kind": "rewrite",
            "file": subtask.target_file, "req": detail,
        }))
        await log(f"[Waiting] Full-rewrite approval required for: {subtask.target_file}...", "log-worker")
        loop = asyncio.get_running_loop()
        approval_future = loop.create_future()
        return await approval_future

    async def execute_task_flow(user_prompt: str):
        nonlocal task_running, task_finished_pending_decision
        task_running = True
        await update_status("processing", "Analyzing code & planning architecture...", 10)
        await log(f"\n[Task Received] {user_prompt}", "log-plan")

        try:
            repo_map = await asyncio.to_thread(indexer.generate_repo_map)
            report = await orchestrator.run_request(
                user_prompt,
                repo_map,
                on_event=on_event,
                on_high_impact_approval=on_high_impact_approval,
                on_rewrite_approval=on_rewrite_approval,
            )

            if report.status == TaskStatus.GUIDANCE_PROVIDED:
                badge, message = _STATUS_TO_BADGE[TaskStatus.GUIDANCE_PROVIDED]
                await update_status(badge, message)
                await websocket.send_text(json.dumps({"type": "guidance", "text": report.guidance.as_text()}))
                await log("[Guidance] No files were changed -- see the guidance panel for what to do manually.", "log-plan")
                return

            if report.files_modified:
                commit_ok, commit_msg = await asyncio.to_thread(
                    sandbox.commit_changes, f"Auto Build: {user_prompt[:72]}"
                )
                if not commit_ok:
                    await log(f"[Git ERROR] {commit_msg}", "log-error")

            diff_out = await asyncio.to_thread(sandbox.diff_against_original)
            await websocket.send_text(json.dumps({"type": "diff", "text": diff_out}))

            badge, message = _STATUS_TO_BADGE.get(report.status, ("failed", report.status.value))
            await update_status(badge, message)
            await log("\n" + report.evidence_text(), "log-success" if badge == "success" else ("log-error" if badge == "failed" else ""))

            if report.files_modified:
                task_finished_pending_decision = True
                await websocket.send_text(json.dumps({"type": "decision_needed"}))
                await log("[Action Required] Review the diff above, then choose Merge or Discard.", "log-plan")
            else:
                await log("[Info] No files were modified -- nothing to merge or discard.", "")

        except Exception as e:
            await update_status("failed", f"Error: {str(e)[:120]}")
            await log(f"[Error] {e}", "log-error")
        finally:
            task_running = False

    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            action = data.get("action")

            if action == "approval_response":
                if approval_future and not approval_future.done():
                    approval_future.set_result(bool(data.get("approved", False)))
                continue

            if action in ("merge", "discard"):
                if not sandbox:
                    await log("[Error] No active session to merge/discard.", "log-error")
                    continue
                if not task_finished_pending_decision:
                    await log("[Error] No pending changes awaiting a merge/discard decision.", "log-error")
                    continue
                ok, msg = await asyncio.to_thread(sandbox.merge if action == "merge" else sandbox.discard)
                await log(("[Git] " if ok else "[Git ERROR] ") + msg, "log-success" if ok else "log-error")
                task_finished_pending_decision = False
                # A fresh session branch is needed for the next task regardless
                # of merge/discard outcome, since the old one is gone/merged.
                sandbox.create_session_branch()
                continue

            if action == "set_model":
                selected_model = data.get("model")
                if orchestrator:
                    try:
                        orchestrator.llm.set_active_model(selected_model)
                        await log(f"[Config] Switched active model to: {selected_model} "
                                  f"({'LOCAL' if orchestrator.llm.is_local() else 'CLOUD'})", "log-plan")
                    except UnknownModelError as e:
                        await log(f"[Error] {e}", "log-error")
                continue

            if action == "init":
                if task_running:
                    await log("[Error] A task is currently running; wait for it to finish before re-initializing.", "log-error")
                    continue

                project_path = data.get("path")
                if not project_path:
                    await log("[Error] No project path provided.", "log-error")
                    continue

                selected_model = data.get("model")
                if not selected_model:
                    await log("[Error] No model selected.", "log-error")
                    continue

                p = Path(project_path).resolve()
                p.mkdir(parents=True, exist_ok=True)

                sandbox = GitSandbox(str(p))
                sandbox.create_session_branch()
                orchestrator = AgentOrchestrator(str(p))
                try:
                    orchestrator.llm.set_active_model(selected_model)
                except UnknownModelError as e:
                    await log(f"[Error] {e}", "log-error")
                    orchestrator = None
                    continue
                indexer = RepoIndexer(str(p))
                task_finished_pending_decision = False

                await update_status("idle", "Agent initialized and ready.")
                await log(
                    f"[Ready] Initialized Auto Build on: {p} "
                    f"(Brain: {selected_model}, {'LOCAL' if orchestrator.llm.is_local() else 'CLOUD -- code and prompts leave this machine'})",
                    "log-success",
                )
                continue

            if action == "task":
                if not orchestrator:
                    await update_status("failed", "Must initialize project first.")
                    await log("[Error] Click 'Initialize / Reset' first.", "log-error")
                    continue
                if task_running:
                    await log("[Error] A task is already running; wait for it to finish.", "log-error")
                    continue
                if task_finished_pending_decision:
                    await log("[Error] Resolve the pending merge/discard decision before starting a new task.", "log-error")
                    continue

                user_prompt = data.get("prompt")
                asyncio.create_task(execute_task_flow(user_prompt))
                continue

    except WebSocketDisconnect:
        pass
