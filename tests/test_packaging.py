"""The built wheel ships the web UI and the `ckdoffline` console script."""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.slow
def test_wheel_contents(tmp_path):
    subprocess.run([sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-q",
                    "-w", str(tmp_path)], check=True)
    wheel = next(tmp_path.glob("ckdoffline-*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())

    web = ROOT / "ckdoffline" / "web"
    expected = {f"ckdoffline/web/{p.relative_to(web).as_posix()}"
                for p in web.rglob("*") if p.is_file()}
    assert expected <= names, sorted(expected - names)
    assert "ckdoffline/__main__.py" in names
    assert not any(n.startswith("tests/") for n in names)

    assert any(n.endswith("/LICENSE") and ".dist-info/" in n for n in names)
    meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
    assert "License-Expression: MIT" in zipfile.ZipFile(wheel).read(meta).decode()

    ep = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
    text = zipfile.ZipFile(wheel).read(ep).decode()
    assert "ckdoffline = ckdoffline.__main__:main" in text
