"""Render role columns from the server registry; report named-check coverage without claiming migration is complete."""
import ast
import csv
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "services/control-plane/app"
ROLES = ["Viewer", "Executor", "Contributor", "Manager", "Admin", "Owner", "PlatformContributor", "DataReader", "DataContributor", "AccessAdministrator"]


def generate(check=False):
    registry = json.loads((APP / "permission_registry.json").read_text())
    wired = {"pages.view"}
    referenced = set()
    for source in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node,ast.Constant) and isinstance(node.value,str) and node.value in registry:
                referenced.add(node.value)
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in ("require", "needs", "allowed", "require_domain", "domain_allowed", "require_query_function", "query_function_allowed"):
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
                     *cells, definition["constraint"], "Direct check" if key in wired else "Action map / engine" if key in referenced else "No literal reference"])
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    matrix = ROOT / "docs/permission-matrix.md"
    before, rest = matrix.read_text().split("## Complete functionality matrix", 1)
    _, after = rest.split("## Page visibility coverage", 1)
    rendered = ["## Complete functionality matrix", "",
                "Generated from the server action registry. **Direct check** identifies a literal permission at a guard call; **Action map / engine** identifies a key used by a dispatcher or compiled data policy. **No literal reference** requires inspecting dynamic domain dispatch or denotes a reserved operation without a separate API. This source-reference column is not a security conformance result or evidence that a reserved operation exists.", "",
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
    print(f"Permission reference: {len(rows)} actions; {len(wired)} direct guard references; {len(referenced)} total source references.")


if __name__ == "__main__":
    generate("--check" in sys.argv)
