"""Keep software validation and explicitly authorized physical acceptance separate."""

from pathlib import Path
import shutil
import sys

import pytest


@pytest.fixture
def installed_plugin(tmp_path):
    """Expose temporary entry-point metadata to an isolated child, without pip/network."""
    root = tmp_path / "installed plugins"
    root.mkdir()

    def install(name, source=None, *, plugin_name=None):
        module = name.replace("-", "_")
        package = root / module
        if source is not None:
            shutil.copytree(source, package)
        else:
            package.mkdir()
            (package / "__init__.py").write_text(
                "from autospan.api import PluginDescriptor, ScenarioContribution\n"
                "class Plugin:\n"
                f"    descriptor = PluginDescriptor({name!r}, '0.1.0', 1)\n"
                "    def register(self, registrar):\n"
                f"        registrar.scenarios(ScenarioContribution({module!r}, relative_path='.'))\n"
                "PLUGIN = Plugin()\n"
            )
        metadata = root / f"{module}-0.1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: {name}\nVersion: 0.1.0\n")
        (metadata / "entry_points.txt").write_text(
            f"[autospan.plugins]\n{plugin_name or name} = {module}:PLUGIN\n"
        )
        command = [
            sys.executable,
            "-I",
            "-c",
            f"import site, runpy; site.addsitedir({str(root)!r}); "
            "runpy.run_module('autospan', run_name='__main__')",
        ]
        return package, command

    return install


def pytest_configure(config: pytest.Config) -> None:
    # Hosted pytest uses AutoSpan's generated ini, not this checkout's pyproject.toml.
    config.addinivalue_line("markers", "unit: software tests for autospan-ble; no physical devices")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    root = Path(__file__).parent
    for item in items:
        if not item.path.is_relative_to(root):
            continue
        folder = item.path.relative_to(root).parts[0]
        if folder == "unit":
            item.add_marker(pytest.mark.unit)
        elif folder == "hardware":
            item.add_marker(pytest.mark.hardware)
            if not config.getoption("autospan_gate_hardware", default=False):
                item.add_marker(
                    pytest.mark.skip(
                        reason="run through autospan test with --hardware and a bench config"
                    )
                )
