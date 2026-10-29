"""The distribution verifier must be safe on every supported Python 3.11 patch."""

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest


def verifier():
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_install.py"
    spec = importlib.util.spec_from_file_location("verify_install", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["../escape", "/escape", "C:/escape", "a\\..\\escape"])
def test_source_extraction_rejects_unsafe_paths_before_writing(tmp_path, name):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        archive.addfile(tarfile.TarInfo("valid.txt"), io.BytesIO())
        archive.addfile(tarfile.TarInfo(name), io.BytesIO())
    data.seek(0)
    with tarfile.open(fileobj=data) as archive, pytest.raises(ValueError, match="Unsafe"):
        verifier().extract_source(archive, tmp_path / "source")
    assert not (tmp_path / "source").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_source_extraction_rejects_links_and_special_files(tmp_path, kind):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        member = tarfile.TarInfo("source/link")
        member.type = kind
        member.linkname = "../../escape"
        archive.addfile(member)
    data.seek(0)
    with tarfile.open(fileobj=data) as archive, pytest.raises(ValueError, match="Unsafe"):
        verifier().extract_source(archive, tmp_path / "source")


def test_source_extraction_preserves_regular_file_contents(tmp_path):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        directory = tarfile.TarInfo("package/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        member = tarfile.TarInfo("package/module.py")
        member.size = 7
        archive.addfile(member, io.BytesIO(b"x = 42\n"))
    data.seek(0)
    with tarfile.open(fileobj=data) as archive:
        verifier().extract_source(archive, tmp_path / "source")
    assert (tmp_path / "source/package/module.py").read_bytes() == b"x = 42\n"
