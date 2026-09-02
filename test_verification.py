import shutil
import tempfile
from pathlib import Path

from verification import (
    ContractRegistry, verify_frontend_contracts, validate_syntax,
    CheckStatus, _paths_structurally_equal, _normalize_path,
)


def make_project(tmp):
    root = Path(tmp)
    (root / "app" / "routes").mkdir(parents=True)
    (root / "app" / "main.py").write_text(
        "from app.routes import orders\n"
        "app.include_router(orders.router, prefix='/api')\n"
    )
    (root / "app" / "routes" / "orders.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter(prefix='/orders')\n"
        "class OrderIn(BaseModel):\n"
        "    item: str\n"
        "    quantity: int\n"
        "@router.get('/')\n"
        "def list_orders():\n"
        "    return []\n"
        "@router.post('/')\n"
        "def create_order(payload: OrderIn):\n"
        "    return {}\n"
        "@router.get('/{order_id}')\n"
        "def get_order(order_id: int):\n"
        "    return {}\n"
    )
    return root


def test_structural_path_matching():
    assert _paths_structurally_equal("/orders/{order_id}", "/orders/123")
    assert not _paths_structurally_equal("/orders/{order_id}", "/orders")
    assert not _paths_structurally_equal("/user", "/users")  # exact segments differ
    assert _normalize_path("/api//orders/") == "/api/orders"
    print("PASS: structural path matching (including /user vs /users false-positive fix)")


def test_registry_extracts_routes():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        reg = ContractRegistry(root)
        paths = sorted((r.method, r.path) for r in reg.routes)
        assert ("GET", "/api/orders") in paths, paths
        assert ("POST", "/api/orders") in paths, paths
        assert ("GET", "/api/orders/{order_id}") in paths, paths
        # request fields attributed to POST /orders
        post_route = next(r for r in reg.routes if r.method == "POST")
        assert set(post_route.request_fields) == {"item", "quantity"}, post_route.request_fields
        print("PASS: registry extracts structured routes with attributed request fields")


def test_static_verifier_catches_wrong_method_and_wrong_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(tmp)
        reg = ContractRegistry(root)
        frontend = root / "app" / "static" / "index.html"
        frontend.parent.mkdir(parents=True)
        frontend.write_text("""
        <script>
        axios.get('/api/orders').then(r => console.log(r));
        axios.get('/api/nonexistent-resource').then(r => {});
        axios.post('/api/orders', {item:'x'});
        fetch('/api/orders', {method: 'DELETE'});
        axios.get(`/api/orders/${orderId}`);
        </script>
        """)
        results = verify_frontend_contracts(frontend, reg)
        by_name = {r.name: r for r in results}

        # correct GET passes
        assert by_name["GET /api/orders"].status == CheckStatus.PASSED

        # correct POST passes
        assert by_name["POST /api/orders"].status == CheckStatus.PASSED

        # DELETE /api/orders has no backend route with that method -> FAILED (path exists, method doesn't)
        assert by_name["DELETE /api/orders"].status == CheckStatus.FAILED
        assert "not for HTTP method" in by_name["DELETE /api/orders"].detail

        # nonsense path -> FAILED, hallucinated-endpoint message
        assert by_name["GET /api/nonexistent-resource"].status == CheckStatus.FAILED
        assert "hallucinated" in by_name["GET /api/nonexistent-resource"].detail

        # a real path-param route DOES structurally match any single-segment
        # value for that slot (e.g. /orders/{order_id} matches /orders/999) --
        # this is correct, not a false positive, since it's genuinely the same route.
        frontend2 = root / "app" / "static" / "index2.html"
        frontend2.write_text("<script>axios.get('/api/orders/999');</script>")
        results2 = verify_frontend_contracts(frontend2, reg)
        assert results2[0].status == CheckStatus.PASSED

        # template literal with interpolation -> UNRESOLVED, not silently passed
        unresolved = [r for r in results if r.status == CheckStatus.UNRESOLVED]
        assert len(unresolved) == 1, results
        print("PASS: static verifier does structural (method,path) matching; unresolved calls reported, not dropped")


def test_no_substring_false_positive_between_user_and_users():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app" / "routes").mkdir(parents=True)
        (root / "app" / "main.py").write_text(
            "app.include_router(users.router, prefix='/api')\n"
        )
        (root / "app" / "routes" / "users.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/users')\n"
            "@router.get('/')\n"
            "def list_users(): return []\n"
        )
        reg = ContractRegistry(root)
        frontend = root / "app" / "static" / "index.html"
        frontend.parent.mkdir(parents=True)
        # /user (singular) does NOT exist; only /users does. Old substring
        # logic would have treated "/user" as found because it's a substring
        # of "/users" in the flattened contracts string.
        frontend.write_text("<script>axios.get('/api/user');</script>")
        results = verify_frontend_contracts(frontend, reg)
        assert results[0].status == CheckStatus.FAILED, results
        print("PASS: /user vs /users no longer produces a false positive")


def test_syntax_validation():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good_py = root / "good.py"
        bad_py = root / "bad.py"
        r1 = validate_syntax(good_py, "def f():\n    return 1\n")
        r2 = validate_syntax(bad_py, "def f(:\n    return 1\n")
        assert r1.status == CheckStatus.PASSED
        assert r2.status == CheckStatus.FAILED
        print("PASS: pre-write syntax validation catches broken Python before it's written")


if __name__ == "__main__":
    test_structural_path_matching()
    test_registry_extracts_routes()
    test_static_verifier_catches_wrong_method_and_wrong_path()
    test_no_substring_false_positive_between_user_and_users()
    test_syntax_validation()
    print("\nALL VERIFICATION UNIT TESTS PASSED")
