"""Prevent beginner-facing function documentation from silently regressing."""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTED_CODE_ROOTS = (
    PROJECT_ROOT / "backend" / "app",
    PROJECT_ROOT / "frontend",
    PROJECT_ROOT / "scripts",
)
DOCUMENTED_SYMBOL_TYPES = (
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def test_every_python_symbol_has_a_docstring() -> None:
    """List every undocumented class/function with a clickable file and line.

    The project is intentionally written as readable learning material.  AST
    inspection includes private helpers, nested validators, callbacks, and
    methods; checking only public functions would allow the hardest pipeline
    steps to become undocumented again.
    """
    missing: list[str] = []

    for root in DOCUMENTED_CODE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, DOCUMENTED_SYMBOL_TYPES):
                    continue
                if ast.get_docstring(node, clean=True) is not None:
                    continue
                relative_path = path.relative_to(PROJECT_ROOT)
                missing.append(f"{relative_path}:{node.lineno} {node.name}")

    assert not missing, "Missing code documentation:\n" + "\n".join(missing)


def test_complex_functions_have_substantive_documentation() -> None:
    """Require more than a label for functions with many inputs or steps.

    A function is treated as complex when it has at least five declared
    parameters (including keyword-only parameters) or spans at least 40 source
    lines.  Fifty characters is intentionally a small floor: it catches
    one-line labels while allowing concise Chinese explanations.
    """
    too_short: list[str] = []

    for root in DOCUMENTED_CODE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                parameter_count = (
                    len(node.args.posonlyargs)
                    + len(node.args.args)
                    + len(node.args.kwonlyargs)
                )
                line_count = (node.end_lineno or node.lineno) - node.lineno + 1
                if parameter_count < 5 and line_count < 40:
                    continue
                documentation = ast.get_docstring(node, clean=True) or ""
                if len(documentation) >= 50:
                    continue
                relative_path = path.relative_to(PROJECT_ROOT)
                too_short.append(
                    f"{relative_path}:{node.lineno} {node.name} "
                    f"(parameters={parameter_count}, lines={line_count})"
                )

    assert not too_short, "Complex code needs fuller documentation:\n" + "\n".join(
        too_short
    )
