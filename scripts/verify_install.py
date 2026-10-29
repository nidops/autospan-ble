"""Verify wheel/sdist installation with PyPI dependencies outside the checkout."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


def run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def extract_source(archive: tarfile.TarFile, destination: Path) -> None:
    """Extract regular source files safely, including on early Python 3.11.

    The tarfile data filter was only backported in Python 3.11.4. Our sdists
    need neither links nor special files; reject those before writing anything.
    """
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or "\\" in member.name
            or ":" in member.name
            or not (member.isdir() or member.isfile())
            or not (destination / member.name).resolve().is_relative_to(destination.resolve())
        ):
            raise ValueError(f"Unsafe source archive member: {member.name!r}")
    for member in members:
        target = destination / member.name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            assert source is not None
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def verify_readme(source: Path, root: Path, python: Path, env: dict[str, str]) -> None:
    """Install the documented user scenario package, without synthetic entry points."""
    markdown = (source / "README.md").read_text(encoding="utf8")
    project = root / "readme-project"
    package = project / "ble_checks"
    package.mkdir(parents=True)
    (project / "fake.yaml").write_text(re.findall(r"```yaml\n(.*?)```", markdown, re.S)[0])
    (project / "pyproject.toml").write_text(re.findall(r"```toml\n(.*?)```", markdown, re.S)[0])
    plugin, fixtures, test = re.findall(r"```python\n(.*?)```", markdown, re.S)
    for name, content in (
        ("__init__.py", plugin),
        ("conftest.py", fixtures),
        ("test_ble.py", test),
    ):
        (package / name).write_text(content)
    host = [str(python), "-I", "-m", "autospan"]
    for arguments in (
        ["plugins"],
        ["plan", "--config", "fake.yaml"],
        ["test", "--config", "fake.yaml", "--", "-q"],
        ["console", "--config", "fake.yaml", "--plain", "--exec", "ble scan scanner 1"],
    ):
        run([*host, *arguments], project, env)
    run([str(python), "-I", "-m", "pip", "install", "-e", "."], project, env)
    run(
        [
            *host,
            "test",
            "--config",
            "fake.yaml",
            "--set",
            "autospan.plugins=[ble, ble-checks]",
            "--",
            "ble_checks/test_ble.py",
            "-q",
        ],
        project,
        env,
    )


def verify_acceptance(source: Path, root: Path, python: Path, env: dict[str, str]) -> None:
    """Install checkout acceptance separately and collect it without acquiring devices."""
    run(
        [
            str(python),
            "-I",
            "-c",
            "from importlib.metadata import entry_points; "
            "assert 'ble-acceptance' not in {ep.name for ep in entry_points(group='autospan.plugins')}",
        ],
        root,
        env,
    )
    run([str(python), "-I", "-m", "pip", "install", "-e", str(source / "tests")], root, env)
    run([str(python), "-I", "-m", "pip", "check"], root, env)
    run([str(python), "-I", "-m", "autospan", "plugins"], root, env)
    arguments = [
        str(python),
        "-I",
        "-m",
        "autospan",
        "test",
        "--config",
        str(source / "autospan.fake.yaml"),
        "--set",
        "autospan.plugins=[ble, ble-acceptance]",
        "--",
        "-m",
        "hardware",
        "-q",
    ]
    result = subprocess.run(
        [*arguments, "--hardware", "--collect-only"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    print(result.stdout, end="")
    if "14/" not in result.stdout and "14 tests" not in result.stdout:
        raise ValueError(f"Expected 14 physical acceptance cases: {result.stdout}")
    result = subprocess.run(
        arguments, cwd=root, env=env, text=True, capture_output=True, check=True
    )
    print(result.stdout, end="")
    if "14 skipped" not in result.stdout:
        raise ValueError("Acceptance must remain skipped without --hardware")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    parser.add_argument("--distribution", choices=("wheel", "sdist"), default="wheel")
    args = parser.parse_args()
    directory = args.directory.resolve()
    wheels, sdists = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        parser.error("Expected one wheel and one sdist")
    wheel, sdist = wheels[0], sdists[0]
    with ZipFile(wheel) as archive:
        (metadata_file,) = (p for p in archive.namelist() if p.endswith(".dist-info/METADATA"))
        version = BytesParser().parsebytes(archive.read(metadata_file))["Version"]

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    for key in list(env):
        if key.startswith("PIP_"):
            env.pop(key)
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    print(
        f"Testing {args.distribution} on {sys.platform} with Python {sys.version.split()[0]}",
        flush=True,
    )

    with tempfile.TemporaryDirectory(prefix="autospan-ble-install-") as temp:
        root = Path(temp)
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip = [str(python), "-I", "-m", "pip"]
        run([*pip, "install", "--upgrade", "pip"], root, env)

        with tarfile.open(sdist, "r:gz") as archive:
            extract_source(archive, root / "source")
        (source,) = (root / "source").iterdir()
        distribution = wheel if args.distribution == "wheel" else sdist
        run([*pip, "install", "--no-cache-dir", f"{distribution}[test]"], root, env)
        run([*pip, "check"], root, env)
        smoke = (
            "import sys, autospan, structwire, autospan_ble; from pathlib import Path; "
            "from importlib.metadata import distribution, version, entry_points; "
            f"assert autospan_ble.__version__ == version('autospan-ble') == {version!r}; "
            "assert all(Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()) "
            "for module in (autospan, structwire, autospan_ble)); "
            "assert all(distribution(name).read_text('direct_url.json') is None "
            "for name in ('autospan', 'structwire')); "
            "print('PyPI dependencies:', {name: version(name) for name in ('autospan', 'structwire')}); "
            "assert Path(autospan_ble.__file__).with_name('py.typed').is_file(); "
            "from autospan_ble import BleCodec, BleGatt, codec; "
            "assert codec(BleCodec.COMPANY_ID).decode(bytes.fromhex('5b07')) == {'company_id': 0x075B}; "
            "assert BleGatt.DEVICE_NAME.value == BleCodec.DEVICE_NAME.value; "
            "ep, = (ep for ep in entry_points(group='autospan.plugins') if ep.name == 'ble'); "
            "assert ep.load().descriptor.version == autospan_ble.__version__; "
            "from autospan.api import AUTOSPAN_PLUGIN_API_VERSION; "
            "assert ep.load().descriptor.api_version == AUTOSPAN_PLUGIN_API_VERSION == 1"
        )
        run([str(python), "-I", "-c", smoke], root, env)
        run([str(python), "-I", "-m", "autospan", "plugins"], root, env)
        run(
            [
                str(python),
                "-I",
                "-m",
                "autospan",
                "test",
                "--config",
                str(source / "autospan.fake.yaml"),
                "--",
                "-q",
            ],
            root,
            env,
        )

        # Pytest must not resolve 'src/' from the unpacked source by configuration.
        # All core imports should originate from the installed distribution.
        run(
            [
                str(python),
                "-I",
                "-m",
                "pytest",
                "--import-mode=importlib",
                "-o",
                "pythonpath=",
                "-o",
                "addopts=-ra --strict-markers",
                "tests",
            ],
            source,
            env,
        )
        verify_readme(source, root, python, env)
        verify_acceptance(source, root, python, env)


if __name__ == "__main__":
    main()
