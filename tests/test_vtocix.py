import pytest

from ckdoffline.errors import UnsupportedError
from ckdoffline.image.base import open_image
from ckdoffline.vtoc.vtoc import Volume
from ckdoffline.vtoc.vtocix import VtocIndex


def _open(path, writable=False):
    return Volume(open_image(path, writable))


def test_not_indexed_refused(fixture_images):
    with pytest.raises(UnsupportedError, match="no active VTOC index"):
        VtocIndex(_open(fixture_images["ckd"]))
