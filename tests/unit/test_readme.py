"""Copy the README configuration/test into an empty directory and run the host."""

import re
import subprocess
from pathlib import Path


def test_readme_fake_bench_and_python_test(tmp_path, installed_plugin):
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    config = re.findall(r"```yaml\n(.*?)```", readme, re.S)[0]
    plugin, fixtures, example = re.findall(r"```python\n(.*?)```", readme, re.S)
    package, command = installed_plugin("ble-checks")
    (package / "__init__.py").write_text(plugin)
    (tmp_path / "fake.yaml").write_text(config)
    (package / "conftest.py").write_text(fixtures)
    (package / "test_ble.py").write_text(example)
    for arguments in (
        ["plugins"],
        ["plan", "--config", "fake.yaml"],
        ["test", "--config", "fake.yaml", "--", "-q"],
        [
            "test",
            "--config",
            "fake.yaml",
            "--set",
            "autospan.plugins=[ble, ble-checks]",
            "--",
            str(package / "test_ble.py"),
            "-q",
        ],
        ["console", "--config", "fake.yaml", "--plain", "--exec", "ble scan scanner 1"],
    ):
        result = subprocess.run(
            [*command, *arguments],
            cwd=tmp_path,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_checkout_acceptance_is_contributed_and_gated(tmp_path, installed_plugin):
    root = Path(__file__).resolve().parents[2]
    # Match the package mapping in tests/pyproject.toml.
    _, command = installed_plugin(
        "autospan-ble-acceptance", root / "tests/hardware", plugin_name="ble-acceptance"
    )
    result = subprocess.run(
        [
            *command,
            "test",
            "--config",
            str(root / "autospan.fake.yaml"),
            "--set",
            "autospan.plugins=[ble, ble-acceptance]",
            "--",
            "-m",
            "hardware",
            "-q",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "14 skipped" in result.stdout
