"""Guard the repository's single application SQL execution boundary."""

import ast
from pathlib import Path


def test_database_execution_is_centralized():
    """Application database execution and psycopg2 SQL builders belong only in orm.py.

    Infrastructure may create pools/cursors in db.py. Test fixture SQL and supplied
    initialization scripts are deliberately outside this application-only rule.
    """
    app = Path(__file__).resolve().parents[2] / "app"
    violations = []
    for path in app.glob("*.py"):
        if path.name == "orm.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"execute", "executemany", "callproc", "mogrify", "SQL"}:
                    violations.append(f"{path.name}:{node.lineno}")
    assert not violations, f"Move database operations to app/orm.py: {violations}"
