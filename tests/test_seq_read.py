import random

import pytest

from ckdoffline.dataset.extents import DatasetIO
from ckdoffline.dataset.records import deblock_stream, reblock
from ckdoffline.dataset.sequential import read_sequential
from ckdoffline.errors import UnsupportedError, ValidationError
from ckdoffline.image.base import open_image
from ckdoffline.vtoc.vtoc import Volume

from oracle import dasdseq


def _vol(path):
    return Volume(open_image(path, False))


def test_fixture_fb_seq(fixture_images):
    for key, img in fixture_images.items():
        recs = read_sequential(_vol(img), "TEST.FB.SEQ")
        assert len(recs) == 500, key
        assert all(len(r) == 80 for r in recs)
        assert recs[0].decode("cp037").startswith("LINE 00001 THE QUICK BROWN FOX A")
        assert b"".join(recs) == dasdseq(img, "TEST.FB.SEQ"), key


def test_fixture_vb_seq(fixture_images):
    # dasdload cannot load VB, so the fixture dataset is empty.
    for key, img in fixture_images.items():
        vol = _vol(img)
        assert read_sequential(vol, "TEST.VB.SEQ") == [], key
        assert list(DatasetIO(vol, "TEST.VB.SEQ").blocks()) == []


def test_datasetio_mapping(fixture_images):
    vol = _vol(fixture_images["ckd"])
    io = DatasetIO(vol, "TEST.FB.SEQ")
    ext = io.extents[0]
    assert io.total_tracks == sum(e.tracks(vol.image.heads) for e in io.extents)
    assert io.rel_to_cchh(0) == (ext.bcyl, ext.bhead)
    first = next(io.blocks())
    assert first[0] == (0, 1)
    tt, r = io.end_of_data()
    assert (tt, r) == io.f1.lstar
    assert io.read_rel_track(0)[0].rec == 1
    with pytest.raises(ValidationError):
        io.rel_to_cchh(io.total_tracks)


def test_reblock_deblock_roundtrip():
    rnd = random.Random(7)
    fb = [bytes(rnd.randrange(256) for _ in range(80)) for _ in range(25)]
    blocks = reblock(fb, "FB", 80, 800)
    assert [len(b) for b in blocks] == [800, 800, 400]
    assert deblock_stream(blocks, "FB", 80) == fb

    vb = [bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 251))) for _ in range(300)]
    blocks = reblock(vb, "VB", 255, 1000)
    assert all(int.from_bytes(b[:2], "big") == len(b) <= 1000 for b in blocks)
    assert deblock_stream(blocks, "VB", 255) == vb

    u = [b"abc", b"\x00" * 100]
    assert reblock(u, "U", 0, 4096) == u
    assert deblock_stream(u, "U", 0) == u


def test_reblock_validation():
    with pytest.raises(ValidationError) as e:
        reblock([b"x" * 80, b"short"], "FB", 80, 800)
    assert e.value.line_no == 2
    with pytest.raises(ValidationError) as e:
        reblock([b"x" * 10, b"y" * 252], "VB", 255, 1000)
    assert e.value.line_no == 2
    with pytest.raises(UnsupportedError):
        reblock([b"x"], "VBS", 255, 1000)


def test_vbs_reassembly():
    def seg(flag, data):
        return (len(data) + 4).to_bytes(2, "big") + bytes([flag, 0]) + data

    def blk(*segs):
        body = b"".join(segs)
        return (len(body) + 4).to_bytes(2, "big") + b"\0\0" + body

    blocks = [blk(seg(0, b"one"), seg(1, b"AAA")), blk(seg(3, b"BBB")),
              blk(seg(2, b"CCC"), seg(0, b"two"))]
    assert deblock_stream(blocks, "VBS", 100) == [b"one", b"AAABBBCCC", b"two"]


def test_blocks_from_past_lstar_is_empty(fixture_images):
    """Records after DS1LSTAR on its own track are not data."""
    img = open_image(fixture_images["ckd"], True)
    vol = Volume(img)
    c, f1 = vol.f1("TEST.FB.SEQ")
    f1.lstar = (0, 5)                        # R6..R14 are still on the track
    img.begin()
    vol.write_dscb(c, f1)
    img.commit()
    io = DatasetIO(vol, "TEST.FB.SEQ")
    assert [t for t, _ in io.blocks()] == [(0, r) for r in range(1, 6)]
    assert [t for t, _ in io.blocks((0, 5))] == [(0, 5)]
    assert list(io.blocks((0, 6))) == []
    assert list(io.blocks((1, 1))) == []
