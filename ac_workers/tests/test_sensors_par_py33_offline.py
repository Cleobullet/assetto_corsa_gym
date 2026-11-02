"""Static checks: sensors_par plugin must stay Python 3.3.5 compatible."""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
PLUGIN_FILES = (
    "car_control.py",
    "drive_state.py",
    "sensors_par.py",
)

_NATIVE_GENERICS = frozenset({"list", "dict", "set", "tuple", "frozenset", "type"})


class Py33ForbiddenVisitor(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.errors = []

    def _add(self, node, kind):
        self.errors.append("{0}:{1}: {2}".format(self.path.name, node.lineno, kind))

    def _check_annotation_node(self, node):
        if node is None:
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            self._add(node, "PEP604 union |")
            self._check_annotation_node(node.left)
            self._check_annotation_node(node.right)
            return
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id in _NATIVE_GENERICS:
                self._add(node, "native generic {0}[...]".format(node.value.id))
        if isinstance(node, ast.BinOp):
            self._check_annotation_node(node.left)
            self._check_annotation_node(node.right)

    def visit_AnnAssign(self, node):
        self._add(node, "AnnAssign")
        self._check_annotation_node(node.annotation)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        if node.returns is not None:
            self._add(node, "function return annotation")
            self._check_annotation_node(node.returns)
        for arg in node.args.args:
            if arg.annotation is not None:
                self._add(arg, "parameter annotation")
                self._check_annotation_node(arg.annotation)
        for arg in node.args.kwonlyargs:
            if arg.annotation is not None:
                self._add(arg, "parameter annotation")
                self._check_annotation_node(arg.annotation)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._add(node, "async def")
        self.generic_visit(node)

    def visit_Await(self, node):
        self._add(node, "await")
        self.generic_visit(node)

    def visit_AsyncFor(self, node):
        self._add(node, "async for")
        self.generic_visit(node)

    def visit_AsyncWith(self, node):
        self._add(node, "async with")
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        self._add(node, "f-string")
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        self._add(node, "walrus NamedExpr")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module == "__future__":
            for alias in node.names:
                if alias.name == "annotations":
                    self._add(node, "from __future__ import annotations")
        self.generic_visit(node)


def _check_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    visitor = Py33ForbiddenVisitor(path)
    visitor.visit(tree)
    return visitor.errors


def test_plugin_files_py33_compatible():
    all_errors = []
    for name in PLUGIN_FILES:
        path = PLUGIN_DIR / name
        assert path.is_file(), "missing plugin file: {0}".format(path)
        all_errors.extend(_check_file(path))
    if all_errors:
        raise AssertionError("Python 3.3 incompatible syntax:\n" + "\n".join(all_errors))


def test_drive_state_parses_like_py33():
    path = PLUGIN_DIR / "drive_state.py"
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")


def test_plugin_controls_skips_vjoy_without_feeder():
    import importlib.util

    path = PLUGIN_DIR / "car_control.py"
    spec = importlib.util.spec_from_file_location("car_control_feeder_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)

    class _StubVJoy:
        def __init__(self, reference=None):
            self.reference = reference
            self.acquired = False
            self.open_calls = 0
            self.update_calls = 0

        def open(self):
            self.open_calls += 1
            self.acquired = True
            return True

        def close(self):
            self.acquired = False
            return True

        def generateJoystickPosition(self, **kwargs):
            return b""

        def update(self, _pos):
            self.update_calls += 1
            return True

    stub_vjoy = _StubVJoy
    with mock.patch.dict(
        sys.modules,
        {
            "vjoy": type(sys)("vjoy"),
            "vjoy_linux": type(sys)("vjoy_linux"),
        },
    ):
        import types

        vjoy_mod = types.ModuleType("vjoy")
        vjoy_mod.vJoy = stub_vjoy
        sys.modules["vjoy"] = vjoy_mod
        spec.loader.exec_module(mod)
        ctrl = mod.Controls()
        assert not ctrl.feeder_enabled()
        ctrl.set_controls(0.0, 0.2, -1.0)
        assert ctrl.vj.open_calls == 0
        assert ctrl.vj.update_calls == 0
        ctrl.enable_feeder()
        ctrl.set_controls(0.0, 0.2, -1.0)
        assert ctrl.vj.open_calls == 1
        assert ctrl.vj.update_calls == 1


def main() -> int:
    test_plugin_files_py33_compatible()
    print("OK test_plugin_files_py33_compatible")
    test_drive_state_parses_like_py33()
    print("OK test_drive_state_parses_like_py33")
    test_plugin_controls_skips_vjoy_without_feeder()
    print("OK test_plugin_controls_skips_vjoy_without_feeder")
    print("Test-SensorsPar-Py33-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
