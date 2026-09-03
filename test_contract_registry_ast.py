import tempfile
from pathlib import Path

from verification import ContractRegistry


def test_non_standard_router_variable_name_is_no_longer_missed():
    """The old regex was hardcoded to the literal string "@router." -- a
    file that named its router anything else (a common real-world pattern,
    e.g. "api_router" or "orders_router") would have every single route in
    it silently invisible to the contract registry. AST tracks the actual
    variable name assigned via `X = APIRouter(...)`."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app" / "routes").mkdir(parents=True)
        (root / "app" / "main.py").write_text(
            "app.include_router(orders.api_router, prefix='/api')\n"
        )
        (root / "app" / "routes" / "orders.py").write_text(
            "from fastapi import APIRouter\n"
            "api_router = APIRouter(prefix='/orders')\n"  # NOT named "router"
            "@api_router.get('/')\n"
            "def list_orders():\n"
            "    return []\n"
        )
        reg = ContractRegistry(root)
        paths = [(r.method, r.path) for r in reg.routes]
        assert ("GET", "/api/orders") in paths, paths
        print("PASS: a router variable not literally named 'router' (e.g. 'api_router') is now correctly tracked")


def test_multiline_decorator_and_signature_resolve_correctly():
    """The exact case regex could not handle: a route decorator whose
    arguments span multiple lines, and a handler signature with each
    parameter on its own line."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app" / "routes").mkdir(parents=True)
        (root / "app" / "main.py").write_text(
            "app.include_router(orders.router, prefix='/api')\n"
        )
        (root / "app" / "routes" / "orders.py").write_text(
            "from fastapi import APIRouter\n"
            "from pydantic import BaseModel\n"
            "router = APIRouter(prefix='/orders')\n"
            "class OrderIn(BaseModel):\n"
            "    item: str\n"
            "    quantity: int\n"
            "@router.post(\n"
            "    '/',\n"
            "    response_model=dict,\n"
            ")\n"
            "async def create_order(\n"
            "    payload: OrderIn,\n"
            ") -> dict:\n"
            "    return {}\n"
        )
        reg = ContractRegistry(root)
        post_route = next((r for r in reg.routes if r.method == "POST"), None)
        assert post_route is not None, reg.routes
        assert post_route.path == "/api/orders", post_route.path
        assert set(post_route.request_fields) == {"item", "quantity"}, post_route.request_fields
        print("PASS: a multi-line decorator + multi-line async signature resolves correctly via AST")


def test_field_types_are_captured_not_just_names():
    """New capability: request_field_types gives the actual type annotation
    text for each field, not just its name -- the old regex-based version
    only ever extracted field names."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app" / "routes").mkdir(parents=True)
        (root / "app" / "main.py").write_text("app.include_router(orders.router, prefix='/api')\n")
        (root / "app" / "routes" / "orders.py").write_text(
            "from fastapi import APIRouter\n"
            "from pydantic import BaseModel\n"
            "from typing import Optional\n"
            "router = APIRouter(prefix='/orders')\n"
            "class OrderIn(BaseModel):\n"
            "    item: str\n"
            "    quantity: int\n"
            "    notes: Optional[str] = None\n"
            "@router.post('/')\n"
            "def create_order(payload: OrderIn):\n"
            "    return {}\n"
        )
        reg = ContractRegistry(root)
        post_route = next(r for r in reg.routes if r.method == "POST")
        assert post_route.request_field_types["item"] == "str"
        assert post_route.request_field_types["quantity"] == "int"
        assert post_route.request_field_types["notes"] == "Optional[str]"
        print("PASS: request_field_types captures real type annotations (str, int, Optional[str]), not just field names")


def test_dynamic_path_is_not_guessed():
    """A path built from a variable (not a string literal) cannot be
    resolved statically -- AST parsing doesn't change that fact, and the
    registry correctly declines to guess rather than fabricating a route."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app" / "routes").mkdir(parents=True)
        (root / "app" / "main.py").write_text("app.include_router(orders.router, prefix='/api')\n")
        (root / "app" / "routes" / "orders.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/orders')\n"
            "DYNAMIC_PATH = '/computed'\n"
            "@router.get(DYNAMIC_PATH)\n"
            "def dynamic_route():\n"
            "    return []\n"
        )
        reg = ContractRegistry(root)
        assert reg.routes == [], reg.routes
        print("PASS: a dynamically-referenced (non-literal) path is not added to the registry rather than guessed")


def test_syntax_error_in_one_route_file_does_not_crash_registry():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app" / "routes").mkdir(parents=True)
        (root / "app" / "main.py").write_text("app.include_router(good.router, prefix='/api')\n")
        (root / "app" / "routes" / "good.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/good')\n"
            "@router.get('/')\n"
            "def ok():\n"
            "    return []\n"
        )
        (root / "app" / "routes" / "broken.py").write_text("def broken(:\n   not valid python\n")

        reg = ContractRegistry(root)
        paths = [(r.method, r.path) for r in reg.routes]
        assert ("GET", "/api/good") in paths, paths
        print("PASS: a syntax error in one routes file doesn't prevent extracting contracts from the other files")


if __name__ == "__main__":
    test_non_standard_router_variable_name_is_no_longer_missed()
    test_multiline_decorator_and_signature_resolve_correctly()
    test_field_types_are_captured_not_just_names()
    test_dynamic_path_is_not_guessed()
    test_syntax_error_in_one_route_file_does_not_crash_registry()
    print("\nALL CONTRACT REGISTRY AST TESTS PASSED")
