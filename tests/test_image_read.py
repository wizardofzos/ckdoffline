import struct

import pytest

from ckdoffline.errors import CkdError, FormatError, UnsupportedError
from ckdoffline.image.base import open_image


def _open(path):
    return open_image(path, writable=False)


def test_all_formats_same_tracks(fixture_images):
    imgs = {k: _open(p) for k, p in fixture_images.items()}
    try:
        for cyl in range(100):
            for head in range(15):
                ref = imgs["ckd"].read_track(cyl, head)
                assert ref[:5] == bytes([0, cyl >> 8, cyl & 255, 0, head])
                assert ref.endswith(b"\xff" * 8)
                for k, img in imgs.items():
                    assert img.read_track(cyl, head) == ref, (k, cyl, head)
    finally:
        for img in imgs.values():
            img.close()


def test_read_track_out_of_range(fixture_images):
    img = _open(fixture_images["ckd"])
    try:
        with pytest.raises(FormatError):
            img.read_track(100, 0)
        with pytest.raises(FormatError):
            img.read_track(0, 15)
    finally:
        img.close()


def test_cache_returns_same_bytes_and_write_refused_read_only(fixture_images):
    img = _open(fixture_images["cckd_zlib"])
    try:
        assert img.read_track(1, 2) == img.read_track(1, 2)
        with pytest.raises(CkdError):
            img.write_track(1, 2, img.read_track(1, 2))
    finally:
        img.close()


def test_bad_magic_and_bigendian_and_devtype(fixture_images, tmp_path):
    bad = tmp_path / "bad"
    bad.write_bytes(b"NOTADASD" + bytes(2000))
    with pytest.raises(FormatError):
        _open(bad)

    be = tmp_path / "be"
    data = bytearray(fixture_images["cckd_zlib"].read_bytes())
    data[512 + 3] |= 0x02
    be.write_bytes(data)
    with pytest.raises(UnsupportedError):
        _open(be)

    dt = tmp_path / "dt"
    data = bytearray(fixture_images["ckd"].read_bytes()[:2048])
    data[16] = 0x80
    dt.write_bytes(data)
    with pytest.raises(UnsupportedError):
        _open(dt)


def _expected_null(trk, fmt, cdh_fmt, heads=15):
    if fmt > 2:
        fmt = cdh_fmt
    elif fmt == 0 and cdh_fmt == 2:
        fmt = 2
    cyl, head = divmod(trk, heads)
    ch = struct.pack(">HH", cyl, head)
    exp = b"\x00" + ch + ch + bytes([0, 0, 0, 8]) + bytes(8)
    if fmt == 0:
        exp += ch + bytes([1, 0, 0, 0])
    elif fmt == 2:
        for r in range(1, 13):
            exp += ch + bytes([r, 0, 0x10, 0]) + bytes(4096)
    return exp + b"\xff" * 8


@pytest.mark.parametrize("key,l2fmt,l2size,nullpos", [
    ("cckd_zlib", "<IHH", 8, 44), ("cckd64", "<QHHI", 16, 72)])
def test_null_tracks_built_per_nullfmt(fixture_images, key, l2fmt, l2size, nullpos):
    """Zero an L2 trkoff and check the synthesised null track."""
    p = fixture_images[key]
    data = bytearray(p.read_bytes())
    l1 = struct.unpack_from("<Q" if key == "cckd64" else "<I", data, 1024)[0]
    cdh_fmt = data[512 + nullpos]
    for fmt in (0, 1, 2, 7):
        args = (0, fmt, fmt) + ((0,) if key == "cckd64" else ())
        struct.pack_into(l2fmt, data, l1 + 3 * l2size, *args)
        p.write_bytes(data)
        img = _open(p)
        try:
            assert img.read_track(0, 3) == _expected_null(3, fmt, cdh_fmt), fmt
        finally:
            img.close()


@pytest.mark.parametrize("key,l1fmt,ones", [
    ("cckd_zlib", "<I", 0xFFFFFFFF), ("cckd64", "<Q", (1 << 64) - 1)])
@pytest.mark.parametrize("value", [0, "ones"])
def test_null_l1_entry(fixture_images, key, l1fmt, ones, value):
    p = fixture_images[key]
    data = bytearray(p.read_bytes())
    struct.pack_into(l1fmt, data, 1024, ones if value == "ones" else 0)
    p.write_bytes(data)
    nullpos = 72 if key == "cckd64" else 44
    img = _open(p)
    try:
        for trk in (0, 17, 255):
            c, h = divmod(trk, 15)
            assert img.read_track(c, h) == _expected_null(trk, 0, data[512 + nullpos])
    finally:
        img.close()


def test_rollback_drops_stale_table_caches(fixture_images):
    p = fixture_images["cckd_zlib"]
    img = open_image(p, writable=True)
    try:
        orig = img.read_track(0, 0)
        l1 = struct.unpack_from("<I", img.raw.read(1024, 4))[0]
        img.begin()
        img.raw.write(l1, struct.pack("<IHH", 0, 1, 1))
        img._invalidate()  # what write_track will do
        changed = img.read_track(0, 0)
        assert changed != orig and changed == _expected_null(0, 1, 0)
        img.rollback()
        assert img.read_track(0, 0) == orig
    finally:
        img.close()


def test_truncated_inside_l1(fixture_images, tmp_path):
    bad = tmp_path / "trunc"
    bad.write_bytes(fixture_images["cckd_zlib"].read_bytes()[:1030])
    with pytest.raises(FormatError):
        _open(bad)
    short = tmp_path / "short"
    short.write_bytes(fixture_images["cckd_zlib"].read_bytes()[:600])
    with pytest.raises(FormatError):
        _open(short)


def _track_off(path, cyl, head):
    img = _open(path)
    try:
        return img._l2_entry(cyl * 15 + head)[0]
    finally:
        img.close()


def test_l2_offset_beyond_eof(fixture_images):
    p = fixture_images["cckd_zlib"]
    data = bytearray(p.read_bytes())
    l1 = struct.unpack_from("<I", data, 1024)[0]
    struct.pack_into("<IHH", data, l1 + 3 * 8, len(data) + 1000, 500, 500)
    p.write_bytes(data)
    img = _open(p)
    try:
        with pytest.raises(FormatError):
            img.read_track(0, 3)
    finally:
        img.close()


@pytest.mark.parametrize("key", ["cckd_zlib", "cckd_bz2"])
def test_corrupt_compressed_stream(fixture_images, key):
    p = fixture_images[key]
    off = _track_off(p, 0, 1)
    data = bytearray(p.read_bytes())
    assert data[off] in (1, 2)
    data[off + 5:off + 5 + 8] = b"\x00\xff" * 4
    p.write_bytes(data)
    img = _open(p)
    try:
        with pytest.raises(FormatError):
            img.read_track(0, 1)
    finally:
        img.close()


@pytest.mark.parametrize("key,pos", [("cckd_zlib", 44), ("cckd64", 72)])
def test_nullfmt2_volume_makes_fmt0_entries_fmt2(fixture_images, key, pos):
    p = fixture_images[key]
    data = bytearray(p.read_bytes())
    data[512 + pos] = 2
    is64 = key == "cckd64"
    l1 = struct.unpack_from("<Q" if is64 else "<I", data, 1024)[0]
    sz = 16 if is64 else 8
    struct.pack_into("<QHHI" if is64 else "<IHH", data, l1 + 3 * sz,
                     *((0, 0, 0, 0) if is64 else (0, 0, 0)))
    p.write_bytes(data)
    img = _open(p)
    try:
        t = img.read_track(0, 3)
    finally:
        img.close()
    assert t == _expected_null(3, 2, 2)
    assert len(t) == 5 + 16 + 12 * (8 + 4096) + 8


def test_nullfmt_above_2_normalised_to_0(fixture_images):
    p = fixture_images["cckd_zlib"]
    data = bytearray(p.read_bytes())
    data[512 + 44] = 9
    struct.pack_into("<I", data, 1024, 0)  # L1[0] null -> uses cdh_nullfmt
    p.write_bytes(data)
    img = _open(p)
    try:
        assert img.read_track(0, 3) == _expected_null(3, 0, 0)
    finally:
        img.close()


def test_comp0_track_with_padding_matches_plain(fixture_images):
    ref_img = _open(fixture_images["ckd"])
    ref = ref_img.read_track(0, 0)
    ref_img.close()
    p = fixture_images["cckd_zlib"]
    img = _open(p)
    free = img.raw.size
    l1 = struct.unpack_from("<I", img.raw.read(1024, 4))[0]
    img.close()
    stored = ref[:5] + ref[5:] + bytes(16)
    data = bytearray(p.read_bytes())
    data += stored  # appended at EOF == offset `free`
    struct.pack_into("<IHH", data, l1, free, len(stored), len(stored))
    p.write_bytes(data)
    img = _open(p)
    try:
        assert img.read_track(0, 0) == ref
    finally:
        img.close()


def test_commit_path_sees_new_value(fixture_images):
    p = fixture_images["cckd_zlib"]
    img = open_image(p, writable=True)
    try:
        l1 = struct.unpack_from("<I", img.raw.read(1024, 4))[0]
        orig = img.read_track(0, 0)
        img.begin()
        img.raw.write(l1, struct.pack("<IHH", 0, 1, 1))
        img.commit()
        assert img.read_track(0, 0) == _expected_null(0, 1, 0) != orig
    finally:
        img.close()


@pytest.mark.parametrize("fileseq,highcyl,ok", [
    (0, 0, True), (1, 0, True), (1, 100, True),        # single file (100 cylinders)
    (1, 49, False), (2, 99, False), (2, 0, False), (0xFF, 99, False),
])
def test_split_ckd_refused(fixture_images, fileseq, highcyl, ok):
    """Final review M6: one file of a split plain-CKD set is refused (ckddasd.c rules)."""
    path = fixture_images["ckd"]
    with open(path, "r+b") as f:
        f.seek(17)
        f.write(bytes([fileseq]) + highcyl.to_bytes(2, "little"))
    if ok:
        open_image(path, False).close()
    else:
        with pytest.raises(UnsupportedError, match="split CKD"):
            open_image(path, False)
