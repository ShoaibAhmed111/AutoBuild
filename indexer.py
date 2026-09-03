# indexer.py
import ast
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", "venv", "__pycache__", ".vite", "dist", "build", ".next", ".idea", "target", ".mvn"}


def _format_decorators(decorator_list) -> list[str]:
    return [f"@{ast.unparse(d)}" for d in decorator_list]


def _format_function_signature(node) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_str = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({args_str}){returns}:"


def _format_class_signature(node: ast.ClassDef) -> str:
    parts = [ast.unparse(b) for b in node.bases]
    parts += [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords]
    bases = f"({', '.join(parts)})" if parts else ""
    return f"class {node.name}{bases}:"


def extract_python_signatures_ast(content: str) -> list[str]:
    """
    Real AST-based extraction (Python only). Unlike line-prefix scanning,
    this correctly handles multi-line decorators and multi-line function
    signatures -- both are reconstructed via ast.unparse() into a single
    clean line regardless of how they were originally formatted, which a
    "does this line start with 'def '" heuristic cannot do (a decorator
    whose arguments span multiple lines, or a signature with each parameter
    on its own line, would previously be silently mangled or missed).

    Also surfaces class-level annotated attributes (e.g. Pydantic model
    fields with their types), which the old indexer never showed at all --
    it only listed function/class headers, not field-level detail.
    """
    tree = ast.parse(content)
    lines: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.extend(_format_decorators(node.decorator_list))
            lines.append(_format_function_signature(node))
        elif isinstance(node, ast.ClassDef):
            lines.extend(_format_decorators(node.decorator_list))
            lines.append(_format_class_signature(node))
            for child in node.body:
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    type_str = ast.unparse(child.annotation)
                    lines.append(f"  {child.target.id}: {type_str}")
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.extend(f"  {d}" for d in _format_decorators(child.decorator_list))
                    lines.append(f"  {_format_function_signature(child)}")

    return lines


class RepoIndexer:
    def __init__(self, root_path: str):
        self.root = Path(root_path)

    def extract_signatures(self, content: str, ext: str) -> list[str]:
        if ext == ".py":
            try:
                return extract_python_signatures_ast(content)
            except SyntaxError:
                # Fall back to the old line-prefix heuristic for THIS file
                # only, so one file with invalid/incomplete syntax (e.g. the
                # LLM is mid-edit, or it's genuinely broken) doesn't crash
                # indexing for the whole repo. This is a degraded fallback,
                # not a silent success -- callers relying on repo_map won't
                # know the difference for this one file, which is a real,
                # acknowledged limitation (see README/reports).
                return self._extract_python_signatures_fallback(content)

        signatures = []
        for line in content.splitlines():
            s = line.strip()
            if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
                continue

            if ext in [".ts", ".tsx", ".js", ".jsx"] and any(s.startswith(k) for k in [
                "export interface", "export type", "export function", "export const", "router.", "app."
            ]):
                signatures.append(s)
            elif ext == ".java" and (
                any(s.startswith(k) for k in ["public class", "public interface", "public enum", "@RestController", "@Service", "@GetMapping", "@PostMapping", "@RequestMapping"])
                or (s.startswith("public ") and "(" in s and "{" in s)
            ):
                signatures.append(s)
        return signatures

    @staticmethod
    def _extract_python_signatures_fallback(content: str) -> list[str]:
        signatures = []
        for line in content.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("def ") or s.startswith("class ") or s.startswith("@"):
                signatures.append(s)
        return signatures

    def generate_repo_map(self) -> str:
        summary_lines = []
        for path in self.root.rglob("*"):
            if path.is_file() and not any(part in path.parts for part in IGNORE_DIRS):
                rel_path = path.relative_to(self.root)
                ext = path.suffix.lower()
                
                if ext in [".py", ".ts", ".tsx", ".js", ".jsx", ".java"]:
                    try:
                        content = path.read_text(encoding="utf-8", errors="ignore")
                        sigs = self.extract_signatures(content, ext)
                        if sigs:
                            summary_lines.append(f"\n[FILE: {rel_path}]")
                            summary_lines.extend(f"  {sig}" for sig in sigs[:40])
                    except Exception:
                        pass
        return "\n".join(summary_lines) if summary_lines else "No source files found."