import ast
import json
from pathlib import Path


source = Path("__init__.py").read_text(encoding="utf-8")
tree = ast.parse(source)
helper_names = {
    "_clean_custom_field_value",
    "_normalise",
    "_unanimous_custom_field_value",
}
helper_nodes = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in helper_names
]
assert {node.name for node in helper_nodes} == helper_names
isolated_module = ast.Module(body=helper_nodes, type_ignores=[])
namespace = {"json": json}
exec(compile(isolated_module, "__init__.py", "exec"), namespace)

clean = namespace["_clean_custom_field_value"]
normalise = namespace["_normalise"]
unanimous = namespace["_unanimous_custom_field_value"]

assert normalise("Paris") == "paris"
assert normalise('"Paris"') == "paris"
assert normalise('  "Paris"  ') == "paris"
assert normalise('"Par\\u0069s"') == "paris"
assert normalise("'Paris'") == "paris"
assert normalise(" Bordeaux ") == "bordeaux"
assert normalise("") is None
assert normalise(None) is None

# The Team Custom Field is filled when at least one member has a campus
# and every non-empty campus agrees.
assert unanimous(["Paris", "Paris"], 2) == "Paris"
assert unanimous(['"Paris"', "paris"], 2) == "Paris"
assert unanimous([" Bordeaux ", "Bordeaux"], 2) == "Bordeaux"
assert unanimous(["Paris", None], 2) == "Paris"
assert unanimous(["Paris", ""], 2) == "Paris"
assert unanimous(["Paris", None, "Paris"], 3) == "Paris"

# Different non-empty campuses conflict; no campus values means no assignment.
assert unanimous(["Paris", "Bordeaux"], 2) is None
assert unanimous(["Paris", "Bordeaux", None], 3) is None
assert unanimous([None, ""], 2) is None
assert unanimous(["Paris"], 0) is None
assert unanimous([], 0) is None
assert clean('"Paris"') == "Paris"

print("Custom Field normalization and team consensus checks passed")
