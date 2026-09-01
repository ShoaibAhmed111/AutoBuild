# indexer.py
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", "venv", "__pycache__", ".vite", "dist", "build", ".next", ".idea", "target", ".mvn"}

class RepoIndexer:
    def __init__(self, root_path: str):
        self.root = Path(root_path)

    def extract_signatures(self, content: str, ext: str) -> list[str]:
        signatures = []
        for line in content.splitlines():
            s = line.strip()
            if not s or s.startswith("//") or s.startswith("#") or s.startswith("/*") or s.startswith("*"):
                continue

            # Python
            if ext == ".py" and (s.startswith("def ") or s.startswith("class ") or s.startswith("@app.")):
                signatures.append(s)
            # TypeScript / JavaScript
            elif ext in [".ts", ".tsx", ".js", ".jsx"] and any(s.startswith(k) for k in [
                "export interface", "export type", "export function", "export const", "router.", "app."
            ]):
                signatures.append(s)
            # Java / Spring Boot
            elif ext == ".java" and (
                any(s.startswith(k) for k in ["public class", "public interface", "public enum", "@RestController", "@Service", "@GetMapping", "@PostMapping", "@RequestMapping"])
                or (s.startswith("public ") and "(" in s and "{" in s)
            ):
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
                            summary_lines.extend(f"  {sig}" for sig in sigs[:20])
                    except Exception:
                        pass
        return "\n".join(summary_lines) if summary_lines else "No source files found."