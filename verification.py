# verification.py
"""
Structured verification subsystem for Auto Build.

Replaces the old "giant contracts string + substring match" approach with
explicit, structured contract records and explicit pass/fail/skip states.

Design goal: never allow SKIPPED, ERROR, NOT_CONFIGURED, or UNRESOLVED to be
read as PASSED/VERIFIED anywhere downstream. Every state is distinct and is
carried through, not collapsed into a boolean.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# Explicit states. Nothing here is allowed to silently become "success".
# --------------------------------------------------------------------------
class CheckStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    ERROR = "ERROR"
    UNRESOLVED = "UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS_PATCH"


class TaskStatus(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    VERIFICATION_NOT_CONFIGURED = "VERIFICATION_NOT_CONFIGURED"
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"
    PLANNING_FAILED = "PLANNING_FAILED"
    GUIDANCE_PROVIDED = "GUIDANCE_PROVIDED"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    detail: str = ""
    source_file: str = ""

    def is_bad(self) -> bool:
        """True for anything that should block a VERIFIED verdict."""
        return self.status in (CheckStatus.FAILED, CheckStatus.ERROR, CheckStatus.AMBIGUOUS)

    def is_incomplete(self) -> bool:
        """True for anything that should block VERIFIED but isn't a hard failure."""
        return self.status in (CheckStatus.SKIPPED, CheckStatus.UNRESOLVED, CheckStatus.NOT_CONFIGURED)


class PlanningFailure(Exception):
    """Raised when the LLM cannot produce a valid, schema-conformant plan
    after retrying. Callers MUST surface this as a visible failure and MUST
    NOT substitute a guessed plan touching arbitrary files."""
    pass


# --------------------------------------------------------------------------
# Structured contract registry (replaces the old giant string blob)
# --------------------------------------------------------------------------
@dataclass
class RouteContract:
    method: str
    path: str  # normalized, e.g. /users/{id}
    request_fields: list = field(default_factory=list)  # field NAMES (kept for backward compatibility)
    request_field_types: dict = field(default_factory=dict)  # field name -> type annotation text (new, AST-derived)
    source_file: str = ""

    def path_segments(self):
        return [p for p in self.path.split("/") if p != ""]


def _normalize_path(p: str) -> str:
    p = re.sub(r"/+", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def _paths_structurally_equal(contract_path: str, called_path: str) -> bool:
    """Exact structural match: same segment count, each segment equal unless
    the contract segment is a FastAPI path parameter ({name}), which matches
    any single literal segment. This is NOT substring containment."""
    a = [s for s in contract_path.split("/") if s != ""]
    b = [s for s in called_path.split("/") if s != ""]
    if len(a) != len(b):
        return False
    for seg_a, seg_b in zip(a, b):
        if seg_a.startswith("{") and seg_a.endswith("}"):
            continue
        if seg_a != seg_b:
            return False
    return True


class ContractRegistry:
    """
    Structured backend route/schema extraction, via real AST parsing.

    NOTE ON HONESTY: this is accurate for FastAPI's common literal patterns
    (APIRouter(prefix="...") with a string literal, @router.get("...") with a
    string literal path, include_router(module.router, prefix="...")). It
    still cannot resolve genuinely dynamic values -- a prefix or path built
    from a variable or f-string at runtime is not statically knowable no
    matter how the source is parsed, AST or otherwise -- and those routes
    are simply not added to the registry rather than guessed at.

    What AST parsing fixes over the previous regex-based version:
    - Multi-line decorators/signatures no longer break extraction (a path
      argument spanning multiple lines, or a decorator with several keyword
      arguments each on their own line, parses correctly).
    - The router variable no longer has to be literally named "router" --
      whatever name is actually assigned via `X = APIRouter(...)` in a file
      is tracked and matched against decorators using that same name. The
      old regex was hardcoded to the literal string "@router." and would
      silently miss every route in a file that named its router anything
      else (e.g. "api_router").
    - Pydantic model field TYPES are now captured (request_field_types), not
      just field names.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.routes: list[RouteContract] = []
        self._build()

    @staticmethod
    def _extract_prefix_from_apirouter_call(call: ast.Call) -> str:
        for kw in call.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
        return ""

    @staticmethod
    def _is_basemodel_subclass(class_def: ast.ClassDef) -> bool:
        for base in class_def.bases:
            if isinstance(base, ast.Name) and base.id == "BaseModel":
                return True
            if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
                return True
        return False

    def _build(self):
        main_py = self.root / "app" / "main.py"
        routes_dir = self.root / "app" / "routes"
        mount_prefixes: dict[str, str] = {}

        if main_py.exists():
            try:
                main_tree = ast.parse(main_py.read_text(encoding="utf-8", errors="ignore"))
                for node in ast.walk(main_tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "include_router"
                        and node.args
                        and isinstance(node.args[0], ast.Attribute)
                        and isinstance(node.args[0].value, ast.Name)
                    ):
                        module_name = node.args[0].value.id
                        mount_prefixes[module_name] = self._extract_prefix_from_apirouter_call(node)
            except SyntaxError:
                pass  # main.py unparsable -- proceed with no mount prefixes rather than crash

        if not routes_dir.exists():
            return

        for py_file in routes_dir.glob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content)
            except (OSError, SyntaxError):
                continue

            router_var_names: set[str] = set()
            local_prefix = ""
            models: dict[str, list[tuple[str, str]]] = {}

            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "APIRouter"
                ):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            router_var_names.add(target.id)
                            local_prefix = self._extract_prefix_from_apirouter_call(node.value) or local_prefix

                elif isinstance(node, ast.ClassDef) and self._is_basemodel_subclass(node):
                    fields: list[tuple[str, str]] = []
                    for child in node.body:
                        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                            fields.append((child.target.id, ast.unparse(child.annotation)))
                    models[node.name] = fields

            if not router_var_names:
                continue  # no APIRouter in this file -- nothing to register

            base_prefix = _normalize_path(mount_prefixes.get(py_file.stem, "") + local_prefix)

            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                for dec in node.decorator_list:
                    if not (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id in router_var_names
                        and dec.func.attr in ("get", "post", "put", "delete", "patch")
                    ):
                        continue
                    if not (dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str)):
                        continue  # dynamically-constructed path -- cannot resolve statically, don't guess

                    verb = dec.func.attr
                    raw_path = dec.args[0].value
                    full_path = _normalize_path(f"{base_prefix}/{raw_path}")

                    request_field_names: list[str] = []
                    request_field_types: dict[str, str] = {}
                    for arg in node.args.args:
                        if arg.annotation is None:
                            continue
                        annotation_text = ast.unparse(arg.annotation)
                        if annotation_text in models:
                            for fname, ftype in models[annotation_text]:
                                request_field_names.append(fname)
                                request_field_types[fname] = ftype
                            break

                    self.routes.append(
                        RouteContract(
                            method=verb.upper(),
                            path=full_path,
                            request_fields=request_field_names,
                            request_field_types=request_field_types,
                            source_file=f"app/routes/{py_file.name}",
                        )
                    )

    def find(self, method: str, path: str) -> Optional[RouteContract]:
        norm = _normalize_path(path)
        for r in self.routes:
            if r.method == method.upper() and _paths_structurally_equal(r.path, norm):
                return r
        return None

    def as_prompt_text(self) -> str:
        """Human/LLM-readable rendering, kept for prompt-building compatibility."""
        if not self.routes:
            return "No backend contracts discovered."
        by_file: dict[str, list[RouteContract]] = {}
        for r in self.routes:
            by_file.setdefault(r.source_file, []).append(r)
        lines = []
        for fname, routes in by_file.items():
            lines.append(f"File: {fname}")
            eps = ", ".join(f"{r.method} {r.path}" for r in routes)
            lines.append(f"  Full API Endpoints: {eps}")
            for r in routes:
                if r.request_fields:
                    field_strs = [
                        f"{name}: {r.request_field_types.get(name, '?')}" for name in r.request_fields
                    ]
                    lines.append(f"    - {r.method} {r.path} fields: [{', '.join(field_strs)}]")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Static frontend-call verifier (structural, not substring)
# --------------------------------------------------------------------------
_AXIOS_RE = re.compile(r"axios\.(get|post|put|delete|patch)\(\s*([`'\"])(.*?)\2", re.DOTALL)
_FETCH_RE = re.compile(
    r"fetch\(\s*([`'\"])(.*?)\1(?:\s*,\s*\{([^}]*)\})?", re.DOTALL
)
_FETCH_METHOD_RE = re.compile(r"method\s*:\s*[`'\"](\w+)[`'\"]")


def _extract_frontend_calls(content: str) -> list[tuple[str, str, bool]]:
    """Returns list of (method, url_or_raw, is_resolved). Unresolved calls
    (template interpolation, concatenation, bare variables) are reported as
    such rather than silently skipped."""
    calls = []
    for method, _quote, raw in _AXIOS_RE.findall(content):
        resolved = "${" not in raw and "+" not in raw
        calls.append((method.upper(), raw, resolved))

    for _quote, raw, opts in _FETCH_RE.findall(content):
        resolved = "${" not in raw and "+" not in raw
        method_match = _FETCH_METHOD_RE.search(opts or "")
        method = method_match.group(1).upper() if method_match else "GET"
        calls.append((method, raw, resolved))

    return calls


def verify_frontend_contracts(frontend_path: Path, registry: ContractRegistry) -> list[CheckResult]:
    """PASS 1, structural: for every statically-resolvable HTTP call in the
    frontend file, requires an exact (method, path) match in the contract
    registry. Unresolved calls are reported as UNRESOLVED, not silently
    dropped and not counted as passed."""
    if not frontend_path.exists():
        return []

    content = frontend_path.read_text(encoding="utf-8", errors="ignore")
    calls = _extract_frontend_calls(content)
    results: list[CheckResult] = []

    for method, raw_url, resolved in calls:
        label = f"{method} {raw_url}"
        if not resolved:
            results.append(
                CheckResult(
                    name=label,
                    status=CheckStatus.UNRESOLVED,
                    detail="URL could not be statically resolved (template interpolation or concatenation); not verified.",
                    source_file=str(frontend_path),
                )
            )
            continue

        clean_path = raw_url.split("?")[0]
        if clean_path == "" or clean_path == "/":
            results.append(CheckResult(name=label, status=CheckStatus.PASSED, source_file=str(frontend_path)))
            continue

        match = registry.find(method, clean_path)
        if match:
            results.append(CheckResult(name=label, status=CheckStatus.PASSED, source_file=str(frontend_path)))
        else:
            # Distinguish "right path wrong method" from "no such path at all"
            # for a clearer failure message (still a hard FAILED either way).
            same_path_any_method = any(_paths_structurally_equal(r.path, _normalize_path(clean_path)) for r in registry.routes)
            if same_path_any_method:
                detail = f"Path '{clean_path}' exists in the backend, but not for HTTP method {method}."
            else:
                detail = f"No backend route matches '{method} {clean_path}'. The frontend may be calling a hallucinated endpoint."
            results.append(CheckResult(name=label, status=CheckStatus.FAILED, detail=detail, source_file=str(frontend_path)))

    return results


# --------------------------------------------------------------------------
# Pre-write syntax validation
# --------------------------------------------------------------------------
def validate_syntax(path: Path, content: str) -> CheckResult:
    ext = path.suffix.lower()
    label = str(path)
    if ext == ".py":
        try:
            ast.parse(content)
            return CheckResult(name=label, status=CheckStatus.PASSED, detail="ast.parse OK")
        except SyntaxError as e:
            return CheckResult(name=label, status=CheckStatus.FAILED, detail=f"Python syntax error: {e}")
    if ext == ".json":
        try:
            json.loads(content)
            return CheckResult(name=label, status=CheckStatus.PASSED, detail="json.loads OK")
        except Exception as e:
            return CheckResult(name=label, status=CheckStatus.FAILED, detail=f"JSON syntax error: {e}")
    if ext in (".html", ".htm"):
        # Lightweight, not a full HTML parser: catch the cheap, common LLM
        # failure mode of unbalanced <script>/<style> tags rather than
        # pretending to fully validate markup.
        for tag in ("script", "style"):
            opens = len(re.findall(rf"<{tag}\b", content, re.IGNORECASE))
            closes = len(re.findall(rf"</{tag}>", content, re.IGNORECASE))
            if opens != closes:
                return CheckResult(
                    name=label, status=CheckStatus.FAILED,
                    detail=f"Unbalanced <{tag}> tags ({opens} open vs {closes} close)."
                )
        return CheckResult(name=label, status=CheckStatus.PASSED, detail="basic tag-balance check OK (not a full HTML validator)")
    # No checker available for this extension: say so, don't claim PASSED.
    return CheckResult(name=label, status=CheckStatus.SKIPPED, detail=f"No syntax checker available for '{ext}'.")


# --------------------------------------------------------------------------
# Runtime verifier (Pass 2)
# --------------------------------------------------------------------------
_DUMMY_VALUES = {
    "id": 1, "user_id": 1, "item_id": 1, "order_id": 1,
    "email": "test@example.com", "name": "Test", "title": "Test",
    "message": "test message", "content": "test content", "text": "test",
}


def _dummy_value(field_name: str):
    fn = field_name.lower()
    if fn in _DUMMY_VALUES:
        return _DUMMY_VALUES[fn]
    if fn.endswith("_id") or fn == "id":
        return 1
    if "email" in fn:
        return "test@example.com"
    if "count" in fn or "num" in fn or "amount" in fn or "quantity" in fn:
        return 1
    if "flag" in fn or fn.startswith("is_") or fn.startswith("has_"):
        return True
    return "test"


def _substitute_path_params(path: str) -> str:
    return re.sub(r"\{[^/}]+\}", "1", path)


def probe_backend_runtime(root: Path, registry: ContractRegistry, allow_destructive: bool = False, timeout: int = 20) -> list[CheckResult]:
    """
    Boots the target FastAPI app in a subprocess and exercises discovered
    routes. Explicit per-route outcomes (PASSED/FAILED/SKIPPED/ERROR) are
    returned -- a boot failure returns a single ERROR result, never PASSED,
    and DELETE routes are SKIPPED by default rather than executed, since this
    process cannot guarantee it is pointed at an isolated test environment.
    """
    schema_bodies = {}
    for r in registry.routes:
        if r.method in ("POST", "PUT", "PATCH"):
            # Keyed on normalized (no trailing slash) path -- the live app's
            # OpenAPI schema may report the same route with a trailing slash
            # (FastAPI routes declared as "/" under a prefix render as
            # "/prefix/"), so the probe script normalizes the same way before
            # doing this lookup.
            schema_bodies[(r.method, _normalize_path(r.path))] = {f: _dummy_value(f) for f in r.request_fields}

    probe_script = f"""
import sys, json
sys.path.insert(0, {str(root)!r})
ALLOW_DESTRUCTIVE = {allow_destructive!r}
SCHEMA_BODIES = {schema_bodies!r}
results = []
try:
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    seen = set()

    # Route discovery via the OpenAPI schema rather than walking app.routes
    # directly: FastAPI's internal route-mounting representation has changed
    # across versions (older versions expose a flat list of APIRoute objects
    # on app.routes; some newer versions wrap included routers lazily and do
    # not resolve to flat APIRoute objects until request time). The OpenAPI
    # schema is a stable, version-independent public surface for "what paths
    # and methods does this app actually expose", so it is used here instead
    # of assuming a particular internal route representation.
    try:
        schema = app.openapi()
        route_pairs = []
        for path, methods_obj in schema.get("paths", {{}}).items():
            for verb in methods_obj.keys():
                route_pairs.append((verb.upper(), path))
    except Exception as e:
        print("BOOT_FAILURE:" + "Could not build OpenAPI schema: " + str(e))
        sys.exit(1)

    for method, path in route_pairs:
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            entry = {{"method": method, "path": path}}
            if method == "DELETE" and not ALLOW_DESTRUCTIVE:
                entry["status"] = "SKIPPED"
                entry["detail"] = "Destructive method skipped by policy (set allow_destructive to enable)."
                results.append(entry)
                continue
            import re as _re
            test_path = _re.sub(r"\\{{[^/}}]+\\}}", "1", path)
            norm_path = path.rstrip("/") or "/"
            try:
                if method == "GET":
                    res = client.get(test_path)
                elif method in ("POST", "PUT", "PATCH"):
                    body = SCHEMA_BODIES.get((method, norm_path), {{}})
                    res = client.request(method, test_path, json=body)
                elif method == "DELETE":
                    res = client.delete(test_path)
                else:
                    entry["status"] = "SKIPPED"
                    entry["detail"] = "Unsupported HTTP method for probing."
                    results.append(entry)
                    continue
                entry["status"] = "PASSED" if res.status_code < 400 else "FAILED"
                entry["detail"] = f"HTTP {{res.status_code}}"
            except Exception as e:
                entry["status"] = "ERROR"
                entry["detail"] = str(e)
            results.append(entry)
    print("RESULTS_JSON:" + json.dumps(results))
    sys.exit(0)
except Exception as e:
    print("BOOT_FAILURE:" + str(e))
    sys.exit(1)
"""
    # Belt-and-suspenders for "import app.main" reliability: the probe
    # script already does sys.path.insert(0, root) internally, but that only
    # helps once the interpreter has started. Explicitly setting cwd=root
    # AND PYTHONPATH here means the probe resolves the target project on the
    # import path from process start, so it doesn't depend on whatever
    # directory server.py/main.py happened to be launched from, or on
    # inherited environment differences between a CLI invocation and a
    # long-running server process.
    probe_env = os.environ.copy()
    existing_pythonpath = probe_env.get("PYTHONPATH", "")
    probe_env["PYTHONPATH"] = (
        f"{root}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(root)
    )
    try:
        res = subprocess.run(
            [sys.executable, "-c", probe_script],
            capture_output=True, text=True, cwd=root, timeout=timeout, env=probe_env,
        )
    except Exception as e:
        # Fail CLOSED: a probe that couldn't even run is an ERROR, not a pass.
        return [CheckResult(name="runtime_probe", status=CheckStatus.ERROR, detail=f"Probe process failed to run: {e}")]

    if res.returncode != 0 or "BOOT_FAILURE" in res.stdout:
        detail = res.stdout.strip() or res.stderr.strip() or "Unknown boot failure."
        return [CheckResult(name="runtime_probe_boot", status=CheckStatus.ERROR, detail=detail)]

    for line in res.stdout.splitlines():
        if line.startswith("RESULTS_JSON:"):
            try:
                raw_results = json.loads(line[len("RESULTS_JSON:"):])
            except Exception as e:
                return [CheckResult(name="runtime_probe_parse", status=CheckStatus.ERROR, detail=f"Could not parse probe output: {e}")]
            out = []
            for r in raw_results:
                status = CheckStatus(r["status"])
                out.append(CheckResult(name=f"{r['method']} {r['path']}", status=status, detail=r.get("detail", "")))
            if not out:
                return [CheckResult(name="runtime_probe", status=CheckStatus.NOT_APPLICABLE, detail="No routes discovered to probe.")]
            return out

    return [CheckResult(name="runtime_probe", status=CheckStatus.ERROR, detail="Probe produced no parseable output.")]
