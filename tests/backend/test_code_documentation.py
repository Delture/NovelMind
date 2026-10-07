"""保护公开接口和复杂流程的教学说明，不以字数或参数数量评判解释质量。"""
import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTED_CODE_ROOTS = (PROJECT_ROOT / "backend" / "app", PROJECT_ROOT / "scripts")


def test_public_interfaces_and_complex_flows_have_documentation():
    """检查模块公开定义与涉及事务/循环/异常的复杂函数，允许短而准确的说明。

    注释是否解释原因由代码审查判断；这里仅防止说明完全消失，不强制嵌套小回调、
    参数数量或固定字符门槛。前端 JavaScript 和兼容教学由人工阅读检查。
    """
    missing = []
    symbols = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for root in DOCUMENTED_CODE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if not isinstance(node, symbols):
                    continue
                complex_flow = sum(isinstance(item, (ast.For, ast.While, ast.Try, ast.With, ast.AsyncWith))
                                   for item in ast.walk(node)) >= 3
                if node.name.startswith("_") and not complex_flow:
                    continue
                if not (ast.get_docstring(node) or "").strip():
                    missing.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} {node.name}")
    assert not missing, "Missing interface/flow explanation:\n" + "\n".join(missing)
