# orchestrator.py
import asyncio
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from llm_client import UniversalLLM
from triage import TaskTier, classify, pick_mechanical_target_file
from verification import (
    CheckResult,
    CheckStatus,
    ContractRegistry,
    PlanningFailure,
    TaskStatus,
    probe_backend_runtime,
    validate_syntax,
    verify_frontend_contracts,
)

MAX_REPAIR_ATTEMPTS = 3


class VerificationResult(BaseModel):
    success: bool
    stage: str
    summary: str
    command: str | None = None
    stdout: str = ""
    stderr: str = ""


class SubTask(BaseModel):
    step_number: int = 1
    target_file: str
    action_type: str = Field(default="MODIFY", description="'CREATE' or 'MODIFY'")
    impact_level: str = Field(default="LOW", description="'LOW' or 'HIGH'")
    requirements: str
    type_definitions: str = Field(default="", description="Signatures/types")


class TaskPlan(BaseModel):
    architectural_summary: str
    verification_command: str = Field(default="none", description="Command to run")
    tasks: list[SubTask]


@dataclass
class WriteOutcome:
    status: str  # "written" | "needs_rewrite_approval" | "error"
    detail: str = ""


@dataclass
class LedgerEvent:
    ts: float
    label: str
    detail: str = ""


@dataclass
class AffectedFile:
    file: str
    why: str
    what_to_change: str


@dataclass
class GuidanceReport:
    """Produced instead of applying changes when a request is classified as
    too risky/cross-cutting to safely auto-apply. One LLM call, no file
    writes, no verification loop -- this is advisory only."""
    summary: str
    affected_files: list = field(default_factory=list)  # list[AffectedFile]
    steps: list = field(default_factory=list)  # list[str]
    raw_text: str = ""

    def as_text(self) -> str:
        lines = [self.summary, ""]
        if self.affected_files:
            lines.append("Likely affected files:")
            for af in self.affected_files:
                lines.append(f"  - {af.file}: {af.why}")
                lines.append(f"      What to change: {af.what_to_change}")
        if self.steps:
            lines.append("\nSuggested steps:")
            for i, s in enumerate(self.steps, 1):
                lines.append(f"  {i}. {s}")
        return "\n".join(lines)


@dataclass
class TaskReport:
    status: TaskStatus = TaskStatus.FAILED
    tier: str = "STANDARD"
    plan_summary: str = ""
    files_modified: list = field(default_factory=list)
    syntax_results: list = field(default_factory=list)
    static_results: list = field(default_factory=list)
    runtime_results: list = field(default_factory=list)
    command_result: Optional[VerificationResult] = None
    repair_attempts: int = 0
    ledger: list = field(default_factory=list)
    guidance: Optional[GuidanceReport] = None

    def evidence_text(self) -> str:
        if self.status == TaskStatus.GUIDANCE_PROVIDED and self.guidance:
            return self.guidance.as_text()
        lines = [
            f"Final: {self.status.value}",
            f"Tier: {self.tier}",
            f"Files modified: {len(self.files_modified)}",
            f"Repair attempts: {self.repair_attempts}",
        ]
        if self.syntax_results:
            lines.append("Syntax checks:")
            lines += [f"  {r.name}: {r.status.value} ({r.detail})" for r in self.syntax_results]
        if self.static_results:
            lines.append("Static contracts:")
            lines += [f"  {r.name}: {r.status.value} ({r.detail})" for r in self.static_results]
        else:
            lines.append("Static contracts: none checked (no frontend files touched)")
        if self.runtime_results:
            lines.append("Runtime:")
            lines += [f"  {r.name}: {r.status.value} ({r.detail})" for r in self.runtime_results]
        if self.command_result:
            tag = "PASS" if self.command_result.success else self.command_result.summary
            lines.append(f"Verification command: {tag}")
        return "\n".join(lines)


class AgentOrchestrator:
    def __init__(self, project_dir: str):
        self.root = Path(project_dir).resolve()
        self.llm = UniversalLLM()

    # ------------------------------------------------------------------
    # Text cleanup helpers
    # ------------------------------------------------------------------
    def _clean_raw_text(self, text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        return text.strip()

    def _strip_code_fence(self, text: str) -> str:
        text = text.strip()
        if "```" in text:
            match = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
            if match:
                return match.group(1).strip()
        if text.startswith("```"):
            lines = text.splitlines()
            body = lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:]
            return "\n".join(body).strip()
        return text.strip("`").strip()

    # ------------------------------------------------------------------
    # Planning -- fail CLOSED. No hardcoded guessed-file fallback.
    # ------------------------------------------------------------------
    def plan(self, repo_map: str, user_goal: str) -> TaskPlan:
        """
        Raises PlanningFailure if the LLM cannot produce a schema-valid plan
        after one repair attempt. Callers MUST treat PlanningFailure as a
        visible failure state (TaskStatus.PLANNING_FAILED) and MUST NOT
        substitute a guessed plan touching arbitrary files.
        """
        registry = ContractRegistry(self.root)
        backend_contracts = registry.as_prompt_text()

        system_prompt = (
            "You are a Principal Software Architect. Plan precise full-stack synchronization. "
            "Never create lazy placeholder stubs. Ensure endpoint routes and frontend field names "
            "match perfectly. Output strictly valid JSON exactly matching the requested schema. "
            "DO NOT invent new keys."
        )

        base_user_prompt = f"""
Codebase Index:
{repo_map}

Existing Backend API Contracts:
{backend_contracts}

Feature Request:
"{user_goal}"

Respond with ONLY this JSON schema:
{{
  "architectural_summary": "Detailed summary",
  "verification_command": "none",
  "tasks": [
    {{
      "step_number": 1,
      "target_file": "app/routes/example.py",
      "action_type": "MODIFY",
      "impact_level": "LOW",
      "requirements": "Precise requirements",
      "type_definitions": "FastAPI APIRouter"
    }}
  ]
}}
"""

        prompt = base_user_prompt
        last_error = None
        attempts = 2  # original attempt + one repair attempt, then STOP

        for attempt in range(1, attempts + 1):
            response_text = self.llm.generate(prompt, system_prompt=system_prompt)
            cleaned = self._clean_raw_text(response_text)
            cleaned = self._strip_code_fence(cleaned) if "```" in cleaned else cleaned
            try:
                start, end = cleaned.find("{"), cleaned.rfind("}")
                if start == -1 or end == -1:
                    raise ValueError("No JSON object found in the LLM's response.")
                data = json.loads(cleaned[start : end + 1])
                return TaskPlan.model_validate(data)
            except Exception as e:
                last_error = e
                prompt = (
                    base_user_prompt
                    + f"\n\nYour previous response was invalid JSON or did not match the schema.\n"
                    f"Error: {e}\nRespond with ONLY the corrected JSON. No prose, no markdown fences."
                )

        raise PlanningFailure(
            f"LLM failed to produce a schema-valid plan after {attempts} attempts. "
            f"Last error: {last_error}. Stopping rather than guessing a plan."
        )

    # ------------------------------------------------------------------
    # Surgical patch engine
    # ------------------------------------------------------------------
    def apply_diff(self, original_content: str, diff_text: str) -> tuple[str, str]:
        """
        Returns (status, payload).
        status in {"applied", "full_text", "ambiguous", "not_found"}.
        "ambiguous" means multiple normalized candidate locations matched and
        this function REFUSES to guess which one -- the caller must escalate
        to human-approved full rewrite rather than silently picking one.
        """
        pattern = re.compile(
            r"<<<<<<<\s*SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>>", re.DOTALL
        )
        matches = list(pattern.finditer(diff_text))

        if not matches:
            clean = self._strip_code_fence(diff_text.strip())
            if "SEARCH" not in clean and len(clean) > len(original_content) * 0.4:
                return "full_text", clean
            return "not_found", original_content

        updated = original_content
        for match in matches:
            search_block, replace_block = match.group(1), match.group(2)

            occurrences = updated.count(search_block)
            if occurrences == 1:
                updated = updated.replace(search_block, replace_block, 1)
                continue
            if occurrences > 1:
                return (
                    "ambiguous",
                    f"SEARCH block matches {occurrences} identical locations verbatim in the file; "
                    f"refusing to guess which one is correct.",
                )
            # occurrences == 0: fall through to whitespace-normalized fallback below

            norm_search = "\n".join(l.strip() for l in search_block.splitlines() if l.strip())
            lines = updated.splitlines()
            window_size = len(search_block.splitlines())
            candidates = []
            for i in range(max(1, len(lines) - window_size + 1)):
                window = "\n".join(l.strip() for l in lines[i : i + window_size] if l.strip())
                if window == norm_search:
                    candidates.append(i)

            if len(candidates) == 0:
                return "not_found", original_content
            if len(candidates) > 1:
                return (
                    "ambiguous",
                    f"SEARCH block matched {len(candidates)} different locations in the file; "
                    f"refusing to guess which one is correct.",
                )

            i = candidates[0]
            updated = updated.replace("\n".join(lines[i : i + window_size]), replace_block, 1)

        return "applied", updated

    # ------------------------------------------------------------------
    # File writing -- syntax-validated before anything touches disk.
    # No silent full-rewrite fallback: ambiguous/not-found patches are
    # returned to the caller as "needs_rewrite_approval", not executed.
    # ------------------------------------------------------------------
    def write_subtask(self, subtask: SubTask, feedback_error: str = "") -> WriteOutcome:
        target_path = self.root / subtask.target_file
        target_path.parent.mkdir(parents=True, exist_ok=True)

        current_content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        error_context = f"\nCRITICAL FIX REQUIRED (Previous error):\n{feedback_error}\n" if feedback_error else ""
        registry = ContractRegistry(self.root)
        is_new = (not target_path.exists()) or len(current_content.strip()) == 0 or subtask.action_type == "CREATE"

        if is_new:
            prompt = (
                f"Target File: {subtask.target_file} (FULL FILE CREATION)\n"
                f"Requirements: {subtask.requirements}\n"
                f"Contracts:\n{registry.as_prompt_text()}\n"
                f"{error_context}\n"
                "Output complete, production-ready code. No placeholders."
            )
        else:
            prompt = (
                f"Target File: {subtask.target_file} (SURGICAL UPDATE)\n"
                f"Requirements: {subtask.requirements}\n"
                f"Contracts:\n{registry.as_prompt_text()}\n"
                f"{error_context}\n"
                f"Existing Code:\n```\n{current_content}\n```\n"
                "Provide exact updates using SEARCH/REPLACE blocks."
            )

        sys_prompt = (
            "You are a Staff Software Engineer. Output clean, surgical diffs or complete code "
            "without code fences. Ensure strict adherence to backend models."
        )

        raw = self.llm.generate(prompt, system_prompt=sys_prompt)
        clean_text = self._clean_raw_text(raw)

        if is_new:
            content = self._strip_code_fence(clean_text)
            syntax = validate_syntax(target_path, content)
            if syntax.status == CheckStatus.FAILED:
                return WriteOutcome("error", f"Generated file failed pre-write syntax check: {syntax.detail}")
            target_path.write_text(content, encoding="utf-8")
            return WriteOutcome("written")

        status, payload = self.apply_diff(current_content, clean_text)

        if status in ("applied", "full_text"):
            content = payload
            syntax = validate_syntax(target_path, content)
            if syntax.status == CheckStatus.FAILED:
                return WriteOutcome("error", f"Patched file failed pre-write syntax check: {syntax.detail}")
            target_path.write_text(content, encoding="utf-8")
            return WriteOutcome("written")

        if status == "ambiguous":
            return WriteOutcome("needs_rewrite_approval", payload)

        return WriteOutcome(
            "needs_rewrite_approval",
            "SEARCH/REPLACE block(s) did not match the current file content; "
            "a full rewrite would be required, which risks discarding existing logic.",
        )

    def force_full_rewrite(self, subtask: SubTask, reason: str = "") -> WriteOutcome:
        """Only call this after explicit human approval -- it is strictly
        riskier than a surgical patch and can discard existing file content."""
        target_path = self.root / subtask.target_file
        current_content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        prompt = (
            f"Rewrite {subtask.target_file} to fulfill:\n{subtask.requirements}\n\n"
            f"A full rewrite was required because: {reason}\n\nCurrent code:\n{current_content}"
        )
        sys_prompt = (
            "You are a Staff Software Engineer. Output clean, complete code without code fences. "
            "Preserve unrelated existing behavior wherever possible."
        )
        raw = self.llm.generate(prompt, system_prompt=sys_prompt)
        content = self._strip_code_fence(self._clean_raw_text(raw))
        syntax = validate_syntax(target_path, content)
        if syntax.status == CheckStatus.FAILED:
            return WriteOutcome("error", f"Full rewrite failed pre-write syntax check: {syntax.detail}")
        target_path.write_text(content, encoding="utf-8")
        return WriteOutcome("written")

    # Backward-compatible shim for any external caller still using the old
    # name. NOT used by run_task's own pipeline (which uses write_subtask +
    # explicit approval for the full-rewrite path).
    def write_task_code(self, subtask: SubTask, feedback_error: str = ""):
        outcome = self.write_subtask(subtask, feedback_error)
        if outcome.status == "needs_rewrite_approval":
            outcome = self.force_full_rewrite(subtask, outcome.detail)
        return outcome

    # ------------------------------------------------------------------
    # Command/test verification
    # ------------------------------------------------------------------
    def run_fixer(self, command: str) -> VerificationResult:
        if not command or command.lower() in ("none", "null", ""):
            return VerificationResult(
                success=False,
                stage="Verification",
                summary="Verification not configured.",
            )
        try:
            res = subprocess.run(
                command, shell=True, capture_output=True, text=True, cwd=self.root, timeout=60
            )
            return VerificationResult(
                success=(res.returncode == 0),
                stage="Verification",
                summary="Verification command executed successfully."
                if res.returncode == 0
                else f"Verification failed with exit code {res.returncode}",
                command=command,
                stdout=res.stdout.strip(),
                stderr=res.stderr.strip(),
            )
        except Exception as e:
            return VerificationResult(
                success=False, stage="Verification", summary=f"Verification execution crashed: {e}", command=command
            )

    # Kept for backward compatibility; superseded by verify_frontend_contracts
    # in verification.py (structural, not substring-based).
    def validate_frontend_contract(self, html_path: str) -> tuple[bool, str]:
        registry = ContractRegistry(self.root)
        results = verify_frontend_contracts(self.root / html_path, registry)
        bad = [r for r in results if r.is_bad()]
        if bad:
            return False, "; ".join(r.detail for r in bad)
        return True, ""

    # ------------------------------------------------------------------
    # Guidance mode (GUIDANCE_ONLY tier): one advisory LLM call, no file
    # writes, no verification loop. Used when triage.classify() decides a
    # request is too risky/cross-cutting to safely auto-apply.
    # ------------------------------------------------------------------
    def generate_guidance(self, user_prompt: str, repo_map: str) -> GuidanceReport:
        registry = ContractRegistry(self.root)
        sys_prompt = (
            "You are a Principal Software Architect providing MIGRATION GUIDANCE, not code. "
            "The requested change has been flagged as too risky or cross-cutting to auto-apply. "
            "Explain precisely which files a human engineer should look at and what to change in "
            "each, and give ordered steps. Do not write full file contents. Output strictly valid JSON."
        )
        prompt = f"""
Codebase Index:
{repo_map}

Existing Backend API Contracts:
{registry.as_prompt_text()}

Feature Request (flagged HIGH RISK -- advisory only):
"{user_prompt}"

Respond with ONLY this JSON schema:
{{
  "summary": "One paragraph explaining the risk and overall approach",
  "affected_files": [
    {{"file": "app/routes/example.py", "why": "...", "what_to_change": "..."}}
  ],
  "steps": ["Step 1 ...", "Step 2 ..."]
}}
"""
        try:
            raw = self.llm.generate(prompt, system_prompt=sys_prompt)
            cleaned = self._clean_raw_text(raw)
            cleaned = self._strip_code_fence(cleaned) if "```" in cleaned else cleaned
            start, end = cleaned.find("{"), cleaned.rfind("}")
            data = json.loads(cleaned[start : end + 1])
            return GuidanceReport(
                summary=data.get("summary", ""),
                affected_files=[
                    AffectedFile(af.get("file", "?"), af.get("why", ""), af.get("what_to_change", ""))
                    for af in data.get("affected_files", [])
                ],
                steps=data.get("steps", []),
                raw_text=raw,
            )
        except Exception as e:
            # Fail soft here (unlike planning): guidance is advisory text for
            # a human, not code that gets executed, so a parse failure just
            # means falling back to showing the raw LLM text rather than
            # structured fields -- it does not risk writing anything to disk.
            return GuidanceReport(
                summary=f"(Could not parse structured guidance: {e}. Showing raw model output below.)",
                raw_text=raw if "raw" in dir() else "",
            )

    # ------------------------------------------------------------------
    # Shared execution core: Write -> Static -> Runtime -> Command ->
    # bounded generalized repair loop -> explicit final TaskStatus.
    # Used by both the STANDARD (planned, multi-file) and MECHANICAL
    # (single pre-selected file, no planning call) tiers so verification
    # behavior never drifts between them.
    # ------------------------------------------------------------------
    async def _execute_and_verify(
        self,
        subtasks: list[SubTask],
        verification_command: str,
        report: TaskReport,
        emit: Callable[[str, str], Awaitable[None]],
        on_high_impact_approval: Optional[Callable[[SubTask], Awaitable[bool]]],
        on_rewrite_approval: Optional[Callable[[SubTask, str], Awaitable[bool]]],
        allow_destructive_probe: bool,
    ) -> TaskReport:
        async def write_with_escalation(subtask: SubTask, feedback: str = "") -> WriteOutcome:
            outcome = await asyncio.to_thread(self.write_subtask, subtask, feedback)
            if outcome.status == "needs_rewrite_approval":
                await emit("REWRITE_APPROVAL_NEEDED", f"{subtask.target_file}: {outcome.detail}")
                approved = await on_rewrite_approval(subtask, outcome.detail) if on_rewrite_approval else False
                if not approved:
                    await emit("REWRITE_REJECTED", subtask.target_file)
                    return WriteOutcome("error", "Full rewrite required but not approved.")
                outcome = await asyncio.to_thread(self.force_full_rewrite, subtask, outcome.detail)
                if outcome.status == "written":
                    await emit("FULL_REWRITE_APPLIED", subtask.target_file)
            return outcome

        for subtask in subtasks:
            if subtask.impact_level.upper() == "HIGH":
                approved = await on_high_impact_approval(subtask) if on_high_impact_approval else True
                if not approved:
                    await emit("SUBTASK_SKIPPED", f"User rejected {subtask.target_file}")
                    continue

            outcome = await write_with_escalation(subtask)

            if outcome.status == "error":
                await emit("SUBTASK_ERROR", f"{subtask.target_file}: {outcome.detail}")
                continue

            report.files_modified.append(subtask.target_file)
            await emit("SUBTASK_EXECUTED", f"{subtask.action_type} -> {subtask.target_file}")

        if not report.files_modified:
            report.status = TaskStatus.FAILED
            await emit("TASK_FINISHED", report.status.value)
            return report

        repair_attempt = 0
        had_static_failure = False
        had_runtime_failure = False
        had_command_failure = False
        static_results: list[CheckResult] = []
        runtime_results: list[CheckResult] = []
        command_result: Optional[VerificationResult] = None

        def _find_subtask(target_file: str) -> SubTask:
            for s in subtasks:
                if s.target_file == target_file:
                    return s
            return SubTask(target_file=target_file, requirements="Fix the reported error.", action_type="MODIFY")

        while True:
            registry = ContractRegistry(self.root)  # rebuild: files may have just changed

            static_results = []
            for f in report.files_modified:
                if f.endswith((".html", ".htm")):
                    static_results.extend(
                        await asyncio.to_thread(verify_frontend_contracts, self.root / f, registry)
                    )
            report.static_results = static_results
            static_failed = [r for r in static_results if r.is_bad()]
            had_static_failure = bool(static_failed)

            if static_failed:
                await emit("STATIC_CHECK_FAILED", "; ".join(r.detail for r in static_failed))
                if repair_attempt >= MAX_REPAIR_ATTEMPTS:
                    report.status = TaskStatus.REPAIR_EXHAUSTED
                    break
                repair_attempt += 1
                await emit(f"REPAIR_ATTEMPT_{repair_attempt}", "Static contract mismatch")
                touched_files = {Path(r.source_file).relative_to(self.root).as_posix() for r in static_failed}
                for f in touched_files:
                    err_text = "; ".join(r.detail for r in static_failed if r.source_file.endswith(f))
                    await write_with_escalation(_find_subtask(f), err_text)
                continue

            await emit("STATIC_CHECK_PASSED", f"{len(static_results)} call(s) checked")

            backend_touched = [f for f in report.files_modified if f.endswith(".py")]
            if backend_touched:
                runtime_results = await asyncio.to_thread(
                    probe_backend_runtime, self.root, registry, allow_destructive_probe
                )
            else:
                runtime_results = [
                    CheckResult(name="runtime_probe", status=CheckStatus.NOT_APPLICABLE, detail="No backend (.py) files were touched this task.")
                ]
            report.runtime_results = runtime_results
            runtime_failed = [r for r in runtime_results if r.is_bad()]
            had_runtime_failure = bool(runtime_failed)

            if runtime_failed:
                await emit("RUNTIME_CHECK_FAILED", "; ".join(f"{r.name}: {r.detail}" for r in runtime_failed))
                if repair_attempt >= MAX_REPAIR_ATTEMPTS:
                    report.status = TaskStatus.REPAIR_EXHAUSTED
                    break
                repair_attempt += 1
                await emit(f"REPAIR_ATTEMPT_{repair_attempt}", "Runtime failure")
                err_text = "\n".join(f"{r.name}: {r.detail}" for r in runtime_failed)
                for f in backend_touched:
                    await write_with_escalation(_find_subtask(f), err_text)
                continue

            await emit("RUNTIME_CHECK_PASSED", f"{len(runtime_results)} route(s) probed")

            command_result = await asyncio.to_thread(self.run_fixer, verification_command)
            report.command_result = command_result
            not_configured = command_result.summary == "Verification not configured."
            had_command_failure = (not command_result.success) and not not_configured

            if had_command_failure:
                await emit("COMMAND_CHECK_FAILED", command_result.stderr or command_result.summary)
                if repair_attempt >= MAX_REPAIR_ATTEMPTS:
                    report.status = TaskStatus.REPAIR_EXHAUSTED
                    break
                repair_attempt += 1
                await emit(f"REPAIR_ATTEMPT_{repair_attempt}", "Verification command failure")
                err_text = (command_result.stdout + "\n" + command_result.stderr).strip()
                for f in backend_touched:
                    await write_with_escalation(_find_subtask(f), err_text)
                continue

            if not_configured:
                await emit("COMMAND_CHECK_NOT_CONFIGURED", command_result.summary)
            else:
                await emit("COMMAND_CHECK_PASSED", command_result.summary)

            break

        report.repair_attempts = repair_attempt

        if report.status == TaskStatus.REPAIR_EXHAUSTED:
            pass
        elif had_static_failure or had_runtime_failure or had_command_failure:
            report.status = TaskStatus.FAILED
        else:
            incomplete = any(r.is_incomplete() for r in static_results + runtime_results)
            cmd_not_configured = command_result is not None and command_result.summary == "Verification not configured."
            if cmd_not_configured and not incomplete:
                report.status = TaskStatus.VERIFICATION_NOT_CONFIGURED
            elif incomplete or cmd_not_configured:
                report.status = TaskStatus.PARTIALLY_VERIFIED
            else:
                report.status = TaskStatus.VERIFIED

        await emit("TASK_FINISHED", report.status.value)
        return report

    # ------------------------------------------------------------------
    # STANDARD tier: full plan -> execute -> verify -> repair.
    # ------------------------------------------------------------------
    async def run_task(
        self,
        user_prompt: str,
        repo_map: str,
        on_event: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_high_impact_approval: Optional[Callable[[SubTask], Awaitable[bool]]] = None,
        on_rewrite_approval: Optional[Callable[[SubTask, str], Awaitable[bool]]] = None,
        allow_destructive_probe: bool = False,
    ) -> TaskReport:
        report = TaskReport(tier=TaskTier.STANDARD.value)

        async def emit(label: str, detail: str = ""):
            report.ledger.append(LedgerEvent(time.time(), label, detail))
            if on_event:
                await on_event(label, detail)

        await emit("TASK_STARTED", user_prompt)

        try:
            plan = await asyncio.to_thread(self.plan, repo_map, user_prompt)
        except PlanningFailure as e:
            await emit("PLANNING_FAILED", str(e))
            report.status = TaskStatus.PLANNING_FAILED
            report.plan_summary = str(e)
            return report

        report.plan_summary = plan.architectural_summary
        await emit("PLAN_GENERATED", plan.architectural_summary)

        return await self._execute_and_verify(
            plan.tasks, plan.verification_command, report, emit,
            on_high_impact_approval, on_rewrite_approval, allow_destructive_probe,
        )

    # ------------------------------------------------------------------
    # MECHANICAL tier: skip multi-file planning entirely. One heuristically
    # (or LLM-)selected target file, one write call, then the SAME
    # verification/repair loop as STANDARD. This is the actual token/time
    # saving lever for small, low-risk, single-file requests.
    # ------------------------------------------------------------------
    async def run_mechanical(
        self,
        user_prompt: str,
        repo_map: str,
        target_file: str,
        on_event: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_high_impact_approval: Optional[Callable[[SubTask], Awaitable[bool]]] = None,
        on_rewrite_approval: Optional[Callable[[SubTask, str], Awaitable[bool]]] = None,
    ) -> TaskReport:
        report = TaskReport(tier=TaskTier.MECHANICAL.value)

        async def emit(label: str, detail: str = ""):
            report.ledger.append(LedgerEvent(time.time(), label, detail))
            if on_event:
                await on_event(label, detail)

        await emit("TASK_STARTED", user_prompt)
        await emit("MECHANICAL_FAST_PATH", f"Skipping multi-file planning; targeting {target_file} directly.")

        subtask = SubTask(
            step_number=1, target_file=target_file, action_type="MODIFY",
            impact_level="LOW", requirements=user_prompt,
        )
        report.plan_summary = f"Mechanical single-file edit: {user_prompt}"

        return await self._execute_and_verify(
            [subtask], "none", report, emit,
            on_high_impact_approval, on_rewrite_approval, False,
        )

    # ------------------------------------------------------------------
    # Front door: triage -> GUIDANCE_ONLY / MECHANICAL / STANDARD.
    # This is what callers (CLI, web server) should invoke instead of
    # run_task() directly, so the cost-saving/safety triage actually runs.
    # ------------------------------------------------------------------
    async def run_request(
        self,
        user_prompt: str,
        repo_map: str,
        on_event: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_high_impact_approval: Optional[Callable[[SubTask], Awaitable[bool]]] = None,
        on_rewrite_approval: Optional[Callable[[SubTask, str], Awaitable[bool]]] = None,
        allow_destructive_probe: bool = False,
    ) -> TaskReport:
        triage_result = classify(user_prompt)

        if on_event:
            await on_event("TRIAGE_DECISION", f"{triage_result.tier.value}: {triage_result.reason}")

        if triage_result.tier == TaskTier.GUIDANCE_ONLY:
            report = TaskReport(tier=TaskTier.GUIDANCE_ONLY.value)
            report.ledger.append(LedgerEvent(time.time(), "TASK_STARTED", user_prompt))
            guidance = await asyncio.to_thread(self.generate_guidance, user_prompt, repo_map)
            report.guidance = guidance
            report.status = TaskStatus.GUIDANCE_PROVIDED
            report.plan_summary = guidance.summary
            if on_event:
                await on_event("GUIDANCE_PROVIDED", guidance.summary)
                await on_event("TASK_FINISHED", report.status.value)
            return report

        if triage_result.tier == TaskTier.MECHANICAL:
            target_file, pick_reason = pick_mechanical_target_file(user_prompt, repo_map)
            if target_file is None:
                if on_event:
                    await on_event("MECHANICAL_FALLBACK", f"{pick_reason} Falling back to full planning (STANDARD tier).")
                return await self.run_task(
                    user_prompt, repo_map, on_event, on_high_impact_approval,
                    on_rewrite_approval, allow_destructive_probe,
                )
            if on_event:
                await on_event("MECHANICAL_FILE_SELECTED", f"{target_file} ({pick_reason})")
            return await self.run_mechanical(
                user_prompt, repo_map, target_file, on_event,
                on_high_impact_approval, on_rewrite_approval,
            )

        return await self.run_task(
            user_prompt, repo_map, on_event, on_high_impact_approval,
            on_rewrite_approval, allow_destructive_probe,
        )
