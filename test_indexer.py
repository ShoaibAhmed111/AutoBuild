import tempfile
from pathlib import Path

from indexer import RepoIndexer, extract_python_signatures_ast


def test_multiline_decorator_and_signature_correctly_captured():
    """The exact case line-prefix regex scanning cannot handle: a decorator
    whose arguments span multiple lines, and a function signature with each
    parameter on its own line. The OLD indexer would have only captured the
    line "@router.get(" (truncated, useless) and completely missed the
    "async def get_user(" line entirely, since it doesn't start with "def "
    (it starts with "async def ", handled below) -- and worse, would have
    picked up stray parameter lines like "    db: Session = Depends(get_db)"
    as noise since they don't match any recognized prefix, or missed the
    signature structure entirely."""
    code = (
        "@router.get(\n"
        '    "/users/{user_id}",\n'
        "    response_model=UserOut,\n"
        ")\n"
        "async def get_user(\n"
        "    user_id: int,\n"
        "    db: Session = Depends(get_db),\n"
        ") -> UserOut:\n"
        "    pass\n"
    )
    sigs = extract_python_signatures_ast(code)
    assert any("router.get" in s and "/users/{user_id}" in s and "response_model=UserOut" in s for s in sigs), sigs
    assert any(s.startswith("async def get_user(") and "-> UserOut" in s for s in sigs), sigs
    print("PASS: a multi-line decorator and multi-line signature are correctly reconstructed as clean single lines via AST")


def test_pydantic_model_fields_are_surfaced_with_types():
    """The old indexer never showed model fields at all -- only function and
    class HEADERS. This is new, real information the repo map didn't
    previously contain."""
    code = (
        "class OrderIn(BaseModel):\n"
        "    item: str\n"
        "    quantity: int\n"
        "    notes: Optional[str] = None\n"
    )
    sigs = extract_python_signatures_ast(code)
    assert "class OrderIn(BaseModel):" in sigs
    assert "  item: str" in sigs
    assert "  quantity: int" in sigs
    assert "  notes: Optional[str]" in sigs
    print("PASS: Pydantic model fields are surfaced with their type annotations (previously invisible to the indexer)")


def test_syntax_error_falls_back_gracefully_not_crash():
    """A file with invalid Python (e.g. mid-edit by the LLM, or just broken)
    must not crash indexing of the entire repo -- it falls back to the old
    line-prefix heuristic for THAT FILE ONLY."""
    broken_code = "def broken(:\n    this is not valid python\n"
    indexer = RepoIndexer("/nonexistent")  # root unused by extract_signatures directly
    sigs = indexer.extract_signatures(broken_code, ".py")
    # fallback heuristic still finds the "def broken(:" line even though it's malformed
    assert any("def broken" in s for s in sigs), sigs
    print("PASS: a Python syntax error falls back to a degraded-but-functional heuristic instead of crashing")


def test_generate_repo_map_handles_mixed_valid_and_broken_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "good.py").write_text(
            "class Foo(BaseModel):\n    x: int\n\ndef bar():\n    pass\n"
        )
        (root / "broken.py").write_text("def broken(:\n   bad syntax here\n")

        indexer = RepoIndexer(str(root))
        repo_map = indexer.generate_repo_map()

        assert "class Foo(BaseModel):" in repo_map
        assert "x: int" in repo_map
        assert "def bar" in repo_map
        # broken.py doesn't crash the whole scan and still contributes what
        # its fallback heuristic could salvage
        assert "broken.py" in repo_map
        print("PASS: generate_repo_map() indexes a mix of valid (AST) and broken (fallback) Python files without crashing")


def test_nested_class_methods_are_indented_and_shown():
    code = (
        "class OrderService:\n"
        "    @staticmethod\n"
        "    def create(item: str) -> dict:\n"
        "        pass\n"
    )
    sigs = extract_python_signatures_ast(code)
    assert "class OrderService:" in sigs
    assert any(s.strip() == "@staticmethod" for s in sigs)
    assert any("def create(item: str) -> dict:" in s for s in sigs)
    print("PASS: methods inside a class are captured (indented) alongside module-level functions")


if __name__ == "__main__":
    test_multiline_decorator_and_signature_correctly_captured()
    test_pydantic_model_fields_are_surfaced_with_types()
    test_syntax_error_falls_back_gracefully_not_crash()
    test_generate_repo_map_handles_mixed_valid_and_broken_files()
    test_nested_class_methods_are_indented_and_shown()
    print("\nALL INDEXER AST TESTS PASSED")
