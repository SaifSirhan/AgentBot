"""PyInstaller spec assets must exist before a build is attempted."""
import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def exe_icons(spec_path):
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    icons = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "EXE":
            continue
        for keyword in node.keywords:
            if keyword.arg == "icon":
                icons.extend(ast.literal_eval(keyword.value))
    return icons


class KestrelSpecTests(unittest.TestCase):
    def test_every_icon_reference_exists(self):
        icons = exe_icons(ROOT / "Kestrel.spec")
        self.assertTrue(icons, "Kestrel.spec declares no icon")
        for icon in icons:
            path = Path(icon)
            if not path.is_absolute():
                path = ROOT / path
            with self.subTest(icon=icon):
                self.assertTrue(path.is_file(), f"missing build icon: {path}")


if __name__ == "__main__":
    unittest.main()
