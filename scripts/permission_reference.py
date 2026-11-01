"""Render role columns from the server registry; report named-check coverage without claiming migration is complete."""
import ast
import csv
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "services/control-plane/app"
ROLES = ["Viewer", "Executor", "Contributor", "Manager", "Admin", "Owner"]


def generate(check=False):
    registry = json.loads((APP / "permission_registry.json").read_text())
    wired = {"pages.view"}
    for source in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in ("require", "needs", "allowed"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in registry:
                    wired.add(arg.value)
    headers = ["Area", "Page / target", "Permission key", "Functionality / action", "Scope", *ROLES,
               "Constraint / recommendation", "Explicit named check"]
    rows = []
    for key, definition in registry.items():
        cells = ["Yes" if role in definition["roles"] else "No" for role in ROLES]
        for role in definition.get("requires_roles", []):
            if role not in definition["roles"]:
                cells[ROLES.index(role)] = "Needs " + " + ".join(r for r in definition["requires_roles"] if r != role)
        rows.append([definition["area"], definition["target"], key, definition["description"], definition["scope"],
                     *cells, definition["constraint"], "Present" if key in wired else "Legacy / pending"])
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    matrix = ROOT / "docs/permission-matrix.md"
    before, rest = matrix.read_text().split("## Complete functionality matrix", 1)
    _, after = rest.split("## Page visibility coverage", 1)
    rendered = ["## Complete functionality matrix", "",
                "Generated from the server action registry. **Present** means an explicit named check was found; **Legacy / pending** means this action still uses existing domain checks or awaits implementation. This column is a migration aid, not proof of complete endpoint/backend coverage.", "",
                "| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        row = list(row)
        row[2] = "`" + row[2] + "`"
        rendered.append("| " + " | ".join(row) + " |")
    content = before + "\n".join(rendered) + "\n\n## Page visibility coverage" + after
    outputs = {ROOT / "docs/permission-matrix.csv": out.getvalue(), matrix: content}
    for path, value in outputs.items():
        if check:
            if path.read_text() != value:
                raise SystemExit(str(path.relative_to(ROOT)) + " is stale; run make docs-reference")
        else:
            path.write_text(value)
    print(f"Permission reference: {len(rows)} actions; {len(wired)} actions with literal named checks. Full migration is not complete.")


if __name__ == "__main__":
    generate("--check" in sys.argv)
