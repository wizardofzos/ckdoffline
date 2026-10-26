import shutil
import subprocess
from pathlib import Path

import pytest

from ckdoffline.image.base import open_image
from ckdoffline.vtoc.dscb import F5Extent, Format5
from ckdoffline.vtoc.vtoc import Volume

from invariants import allocated_tracks

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / ".oracle" / "fixtures"
HEADS = 15
FIXTURE_TRACKS = 100 * HEADS

_NAMES = {
    "ckd": "test01.ckd",
    "cckd_zlib": "test01.cckd_zlib",
    "cckd_bz2": "test01.cckd_bz2",
    "cckd64": "test01.cckd64",
}


def _clone(src: Path, dst: Path) -> None:
    """APFS clone when possible, plain copy otherwise."""
    if subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True).returncode != 0:
        shutil.copyfile(src, dst)


@pytest.fixture(scope="session")
def _fixture_master():
    if not all((FIXTURE_DIR / n).exists() for n in _NAMES.values()):
        subprocess.run([str(ROOT / "tools" / "make_fixtures.sh")], check=True,
                       capture_output=True)
    return FIXTURE_DIR


@pytest.fixture
def fixture_images(_fixture_master, tmp_path):
    """Per-test copies of the synthetic images: {"ckd","cckd_zlib","cckd_bz2","cckd64"} -> Path."""
    out = {}
    for key, name in _NAMES.items():
        dst = tmp_path / name
        _clone(_fixture_master / name, dst)
        out[key] = dst
    return out


def _ranges(tracks):
    out = []
    for t in sorted(tracks):
        if out and out[-1][1] == t:
            out[-1][1] = t + 1
        else:
            out.append([t, t + 1])
    return [tuple(r) for r in out]


@pytest.fixture
def f5_image(fixture_images):
    """The dasdload fixture with a valid free-space map: dasdload leaves the
    DOS bit on and an empty F5 (format 5 not maintained), so build the F5 from
    the extents and clear the DOS bit, as a non-indexed z/OS VTOC would be."""
    path = fixture_images["cckd_zlib"]
    img = open_image(path, True)
    vol = Volume(img)
    free = set(range(FIXTURE_TRACKS)) - allocated_tracks(vol)
    e = vol.vtoc_extent
    f5c = (e.bcyl, e.bhead, 2)
    f5 = vol.read_dscb(f5c)
    assert isinstance(f5, Format5) and not f5.extents
    f5.extents = [F5Extent(s, *divmod(n - s, HEADS)) for s, n in _ranges(free)]
    f4 = vol.read_dscb(vol.vtoc_cchhr)
    assert f4.vtoci == 0x80
    f4.vtoci = 0
    img.begin()
    vol.write_dscb(f5c, f5)
    vol.write_dscb(vol.vtoc_cchhr, f4)
    img.commit()
    img.close()
    return path
