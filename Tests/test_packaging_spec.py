"""打包规格自检：excludes 必须是真实模块名，随包发布的工具必须显式打入。

`can_host.spec` 的 excludes 里曾长期写着 `canhost.simulator`；那个模块在
`canhost/bms/simulator.py` 重命名之后就不存在了，规则一直是空转的，直到
核对冻结包的模块清单才发现。这里把两条都固化成测试：excludes 里的名字必须
能解析到真实模块，两个发布包都必须显式带上本地遥测模拟器和 pyserial。
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS = ("can_host.spec", "can_host_macos.spec")
REQUIRED_IN_BOTH = {"canhost.telemetry.simulator", "serial"}


def analysis_lists(name: str, *keys: str) -> dict[str, list[str]]:
    """取出 spec 里 ``Analysis(...)`` 指定的关键字参数。

    spec 里同时有 ``str(package / ...)`` 这类表达式，所以只解析需要的关键字；
    它们必须是字面量字符串列表，否则本测试无法核对，这时直接失败而不是静默跳过。
    """
    tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            found: dict[str, list[str]] = {}
            for keyword in node.keywords:
                if keyword.arg not in keys:
                    continue
                try:
                    found[keyword.arg] = list(ast.literal_eval(keyword.value))
                except ValueError as error:
                    raise AssertionError(
                        f"{name} 的 {keyword.arg} 不是字面量列表，测试无法核对：{error}"
                    ) from error
            return found
    raise AssertionError(f"{name} 里没有 Analysis(...) 调用")


def module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class PackagingSpecTest(unittest.TestCase):
    def test_excludes_only_name_existing_modules(self) -> None:
        for spec in SPECS:
            for module in analysis_lists(spec, "excludes", "hiddenimports").get("excludes") or []:
                with self.subTest(spec=spec, module=module):
                    self.assertTrue(module_exists(module),
                                    f"{spec} 排除了不存在的模块 {module}，这条规则不会生效")

    def test_both_packages_ship_local_telemetry_publisher(self) -> None:
        for spec in SPECS:
            keywords = analysis_lists(spec, "excludes", "hiddenimports")
            hidden = set(keywords.get("hiddenimports") or [])
            excluded = set(keywords.get("excludes") or [])
            with self.subTest(spec=spec):
                self.assertFalse(REQUIRED_IN_BOTH & excluded,
                                 f"{spec} 排除了本地遥测模拟器的依赖 {sorted(REQUIRED_IN_BOTH & excluded)}")
                self.assertTrue(REQUIRED_IN_BOTH <= hidden,
                                f"{spec} 未显式打入 {sorted(REQUIRED_IN_BOTH - hidden)}")

    def test_windows_package_keeps_can_debug_simulation_out(self) -> None:
        """Windows 发布版是硬件专用的：不打包调试模拟通道，界面与后端也都不提供。"""
        self.assertIn("canhost.vehicle.simulator",
                      analysis_lists("can_host.spec", "excludes").get("excludes") or [])

    def test_macos_build_publishes_the_in_app_update_archive(self) -> None:
        script = (ROOT / "build_macos.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("-update.zip", script)
        self.assertIn("ditto -c -k --sequesterRsrc --keepParent", script)
        self.assertIn('shasum -a 256 "$UPDATE_ZIP_NAME"', script)
        self.assertIn("预期 7 个双平台发布附件", workflow)
        self.assertLess(script.index("scripts/set_version.py"),
                        script.index("-m unittest discover"),
                        "macOS 构建必须先刷新随包版本说明再运行测试")


if __name__ == "__main__":
    unittest.main()
