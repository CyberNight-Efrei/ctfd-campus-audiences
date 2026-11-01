import ast
import json
from pathlib import Path


source = Path("__init__.py").read_text(encoding="utf-8")
tree = ast.parse(source)
normalise_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "_normalise"
)
isolated_module = ast.Module(body=[normalise_node], type_ignores=[])
namespace = {"json": json}
exec(compile(isolated_module, "__init__.py", "exec"), namespace)
normalise = namespace["_normalise"]


assert normalise("Paris") == "paris"
assert normalise('"Paris"') == "paris"
assert normalise('  "Paris"  ') == "paris"
assert normalise('"Par\\u0069s"') == "paris"
assert normalise("'Paris'") == "paris"
assert normalise(" Bordeaux ") == "bordeaux"
assert normalise("") is None
assert normalise(None) is None

print("Custom Field normalization checks passed")
