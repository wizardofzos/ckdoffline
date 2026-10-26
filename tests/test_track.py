
from ckdoffline.image.base import open_image, trim_track
from ckdoffline.track import (EOF_RECORD, GEOM_3390, Record, build_track, fits_on_track,
                              is_eof, parse_track, track_balance)


def test_roundtrip_every_fixture_track(fixture_images):
    img = open_image(fixture_images["ckd"], False)
    try:
        for cyl in range(img.cyls):
            for head in range(img.heads):
                raw = img.read_track(cyl, head)
                r0, recs = parse_track(raw)
                assert build_track(cyl, head, recs, r0) == trim_track(raw, "t")
    finally:
        img.close()


def test_parse_fields_and_eof():
    recs = [Record(3, 4, 1, b"K" * 8, b"D" * 5), Record(3, 4, 2, b"", b"")]
    r0, out = parse_track(build_track(3, 4, recs))
    assert r0 == b"\0" * 8 and out == recs
    assert not is_eof(out[0]) and is_eof(out[1])
    assert EOF_RECORD == (0, 0)


def test_geometry_constants():
    assert (GEOM_3390.heads, GEOM_3390.trksize_hercules, GEOM_3390.max_track_capacity) == (15, 56832, 56664)


def test_capacity_3390():
    assert fits_on_track([(0, 56664)])
    assert not fits_on_track([(0, 56665)])
    assert fits_on_track([(0, 27998)] * 2)
    assert not fits_on_track([(0, 27999)] * 2)
    assert fits_on_track([(0, 27920)] * 2)
    assert fits_on_track([(8, 256)] * 45)
    assert not fits_on_track([(8, 256)] * 46)


def test_track_balance_full_track_is_zero():
    assert track_balance([(0, 56664)]) == 0
    assert track_balance([]) == GEOM_3390.track_length
