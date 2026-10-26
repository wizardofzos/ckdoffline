import hashlib
import random
import re
import struct
import subprocess
import zlib

import pytest

from oracle import ORACLE_BIN, cckdcdsk, dasdls
from ckdoffline.errors import CkdError, FormatError, NoSpaceError
from ckdoffline.image.base import open_image

CCKD_KEYS = ["cckd_zlib", "cckd_bz2", "cckd64"]
ENDTRK = b"\xff" * 8


def build_track(cyl, head, records):
    """Track image: HA, R0 (8 zero bytes), records [(rec, key, data)], end-of-track."""
    ch = struct.pack(">HH", cyl, head)
    out = bytearray(b"\x00" + ch)
    out += ch + b"\x00\x00" + struct.pack(">H", 8) + bytes(8)
    for rec, key, data in records:
        out += ch + bytes([rec, len(key)]) + struct.pack(">H", len(data)) + key + data
    return bytes(out + ENDTRK)


def first_data_record(img):
    """Offset of the first data byte of the first record after R0 that has data."""
    pos = 5 + 8 + 8
    while img[pos:pos + 8] != ENDTRK:
        kl, dl = img[pos + 5], struct.unpack_from(">H", img, pos + 6)[0]
        if dl:
            return pos + 8 + kl
        pos += 8 + kl + dl
    return None


def flip_one_data_byte(img):
    pos = first_data_record(img)
    assert pos is not None
    out = bytearray(img)
    out[pos] ^= 0x5A
    return bytes(out)


def random_track(rng, cyl, head, trksize):
    """Random valid track; sizes from a few bytes to nearly a full track,
    incompressible or very compressible."""
    room = trksize - (5 + 16 + 8)
    kind = rng.random()
    if kind < 0.25:
        recs = [(1, b"", rng.randbytes(rng.randint(1, 80)))]
    elif kind < 0.5:
        n = rng.randint(1, 12)
        recs = [(r, b"", bytes([r]) * rng.randint(100, 4000)) for r in range(1, n + 1)]
    elif kind < 0.75:
        recs = [(1, b"K" * 8, rng.randbytes(rng.randint(5000, room - 16)))]
    else:
        recs = [(r, b"", rng.randbytes(rng.randint(200, 3000))) for r in range(1, rng.randint(2, 10))]
    return build_track(cyl, head, recs)


def all_tracks(path):
    img = open_image(path, writable=False)
    try:
        return {(c, h): img.read_track(c, h) for c in range(img.cyls) for h in range(img.heads)}
    finally:
        img.close()


def check_space(path):
    img = open_image(path, writable=False)
    try:
        img.check_space()
    finally:
        img.close()


@pytest.mark.parametrize("key", ["ckd"] + CCKD_KEYS)
def test_write_readback(fixture_images, key):
    path = fixture_images[key]
    before = all_tracks(path)
    assert first_data_record(before[(5, 3)]) is None  # (5,3) is empty: add a record
    new = {
        (5, 3): build_track(5, 3, [(1, b"", b"HELLO" * 20)]),
        (1, 5): flip_one_data_byte(before[(1, 5)]),  # TEST.FB.SEQ data
    }
    img = open_image(path, writable=True)
    try:
        img.begin()
        for (c, h), t in new.items():
            img.write_track(c, h, t)
        for (c, h), t in new.items():
            assert img.read_track(c, h) == t  # sees the pending write
        img.commit()
        for (c, h), t in new.items():
            assert img.read_track(c, h) == t
    finally:
        img.close()
    after = all_tracks(path)
    for trk, t in before.items():
        assert after[trk] == new.get(trk, t), trk
    if key != "ckd":
        rc, out = cckdcdsk(path)
        assert rc == 0, out
        check_space(path)
    else:
        assert path.stat().st_size == 512 + 1500 * 56832


@pytest.mark.parametrize("key", CCKD_KEYS)
def test_cckdcdsk_clean_after_writes(fixture_images, key):
    path = fixture_images[key]
    listing = dasdls(path)
    model = all_tracks(path)
    rng = random.Random(f"task5-{key}")
    # A small pool of tracks rewritten again and again with random sizes, so
    # tracks grow and shrink, free blocks are reused, split and merged.
    pool = rng.sample([(c, h) for c in range(2, 100) for h in range(15)], 60)
    start = path.stat().st_size
    appended = 0  # bytes the file would grow by if no free space were reused
    for txn in range(5):
        img = open_image(path, writable=True)
        try:
            img.begin()
            for _ in range(40):
                c, h = rng.choice(pool)
                t = random_track(rng, c, h, img.trksize)
                img.write_track(c, h, t)
                model[(c, h)] = t
                appended += min(len(t), 5 + len(zlib.compress(t[5:])))
                assert img.read_track(c, h) == t
            img.commit()
        finally:
            img.close()
        rc, out = cckdcdsk(path)
        assert rc == 0, (txn, out)
        check_space(path)
        assert all_tracks(path) == model
    assert path.stat().st_size - start < appended // 2  # free space was reused
    out = dasdls(path)
    assert out == listing
    for dsn in ["TEST.FB80.PDS", "TEST.VB.PDS", "TEST.LOAD", "TEST.FB.SEQ", "TEST.VB.SEQ"]:
        assert dsn in out


def _free_blocks(path):
    """(cdh_size, free_off, free_num) from the compressed device header."""
    with open(path, "rb") as f:
        hdr = f.read(1024)
    if hdr[:8] == b"CKD_C064":
        size, _used, foff, _tot, _lg, num = struct.unpack_from("<QQQQQq", hdr, 512 + 16)
        return size, foff, num, 16
    size, _used, foff, _tot, _lg, num = struct.unpack_from("<IIIIIi", hdr, 512 + 12)
    return size, foff, num, 8


@pytest.mark.parametrize("key", ["cckd_zlib", "cckd64"])
def test_trailing_free_space_truncated(fixture_images, key):
    """When the space at the end of the file is released, cckd_flush_space
    gives it back: cdh_size goes down and the file is truncated."""
    path = fixture_images[key]
    rng = random.Random(7)

    def put(*writes):
        img = open_image(path, writable=True)
        img.begin()
        for h, data in writes:
            img.write_track(50, h, build_track(50, h, [(1, b"", data)]))
        img.commit()
        img.close()
        rc, out = cckdcdsk(path)
        assert rc == 0, out
        check_space(path)
        return path.stat().st_size

    peak = put((1, rng.randbytes(40000)), (2, rng.randbytes(40000)))  # A, B at the end
    put((1, b"a" * 50))   # A small, at the end; A's old space is a hole
    put((1, b"b" * 50))   # A again: into the hole; its old space was last -> dropped
    final = put((2, b"c" * 50))  # B into the hole; B was last -> dropped, merged with the hole
    assert final < peak - 79000
    size, _foff, num, fbs = _free_blocks(path)
    assert final in (size, size + (num + 1) * fbs)


def _scatter_small_holes(path):
    """Leave many small, non-adjacent free blocks, too small to hold the free
    space table, so Hercules' rule puts the table at the end of the file."""
    img = open_image(path, writable=True)
    img.begin()
    for h in range(15):
        img.write_track(60, h, build_track(60, h, [(1, b"", bytes([h]) * 30)]))
    img.commit()
    img.begin()
    rng = random.Random(3)
    for h in range(0, 15, 2):  # move every other one away
        img.write_track(60, h, build_track(60, h, [(1, b"", rng.randbytes(3000))]))
    img.commit()
    img.close()


@pytest.mark.parametrize("key", ["cckd_zlib", "cckd64"])
def test_free_table_at_end_of_file(fixture_images, key):
    path = fixture_images[key]
    _scatter_small_holes(path)
    size, foff, num, fbs = _free_blocks(path)
    assert num >= 8 and foff == size
    assert path.stat().st_size == size + (num + 1) * fbs
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)
    # The next transaction reads the table at the end and must drop it.
    img = open_image(path, writable=True)
    img.begin()
    img.write_track(61, 0, build_track(61, 0, [(1, b"", b"y" * 50)]))
    img.commit()
    img.close()
    size2, foff2, num2, _ = _free_blocks(path)
    assert path.stat().st_size in (size2, size2 + (num2 + 1) * fbs)
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)


@pytest.mark.parametrize("key", ["cckd_zlib", "cckd64"])
def test_longer_gap_after_table_at_end_accepted(fixture_images, key):
    """cckdutil.c ignores any gap from cdh_size to EOF when free_off ==
    cdh_size (e.g. an older, longer table); so do we, and we truncate it."""
    path = fixture_images[key]
    _scatter_small_holes(path)
    with open(path, "ab") as f:
        f.write(b"\xee" * 100)
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)
    img = open_image(path, writable=True)
    img.begin()
    img.write_track(61, 0, build_track(61, 0, [(1, b"", b"y" * 50)]))
    img.commit()
    img.close()
    size, _foff, num, fbs = _free_blocks(path)
    assert path.stat().st_size in (size, size + (num + 1) * fbs)
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)


@pytest.mark.parametrize("key", ["cckd_zlib", "cckd64"])
def test_old_format_free_chain(fixture_images, key, monkeypatch):
    """Hercules falls back to the old in-block chain when the table cannot be
    appended (file at its maximum size); it must be read back as well."""
    from ckdoffline.image import cckdspace
    path = fixture_images[key]
    _scatter_small_holes(path)
    size, _foff, _num, _ = _free_blocks(path)
    img = open_image(path, writable=True)
    img.begin()
    # Too big for any hole (~34 bytes), so it lands at the end; then the
    # table no longer fits below the (patched) maximum file size.
    t = build_track(61, 5, [(1, b"", b"z" * 31 + random.Random(5).randbytes(20))])
    img.write_track(61, 5, t)
    monkeypatch.setattr(cckdspace, "max_file_size", lambda is64: size + 100)
    img.commit()
    img.close()
    monkeypatch.undo()
    size, foff, num, fbs = _free_blocks(path)
    with open(path, "rb") as f:
        f.seek(foff)
        assert f.read(8) != b"FREE_BLK"
    assert path.stat().st_size == size
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)
    model = all_tracks(path)
    img = open_image(path, writable=True)
    img.begin()
    t = build_track(62, 2, [(1, b"", b"w" * 400)])
    img.write_track(62, 2, t)
    model[(62, 2)] = t
    img.commit()
    img.close()
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)
    assert all_tracks(path) == model


@pytest.mark.parametrize("tool", ["dasdinit", "dasdinit64"])
@pytest.mark.parametrize("linux", [False, True])
def test_new_l2_table_keeps_null_tracks(tmp_path, tool, linux):
    path = _dasdinit(tmp_path, tool, linux)
    before = all_tracks(path)
    img = open_image(path, writable=True)
    assert img._l1[1] == 0 and img._l1[2] == 0  # groups 1 and 2 have no L2 table
    img.begin()
    new = {(20, 7): build_track(20, 7, [(1, b"", b"A" * 100)]),   # track 307, group 1
           (35, 2): build_track(35, 2, [(1, b"", b"B" * 9000)]),  # track 527, group 2
           (35, 3): build_track(35, 3, [(1, b"", b"C" * 10)])}
    for (c, h), t in new.items():
        img.write_track(c, h, t)
    assert img._l1[1] != 0 and img._l1[2] != 0
    for trk, t in before.items():
        assert img.read_track(*trk) == new.get(trk, t), trk
    img.commit()
    img.close()
    after = all_tracks(path)
    for trk, t in before.items():
        assert after[trk] == new.get(trk, t), trk
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)


def test_header_openrw_set_and_totals(fixture_images):
    path = fixture_images["cckd_zlib"]
    with open(path, "r+b") as f:  # start from a header without OPENRW
        f.seek(512 + 3)
        f.write(b"\x00")
    img = open_image(path, writable=True)
    img.begin()
    img.write_track(10, 0, build_track(10, 0, [(1, b"", b"Q" * 500)]))
    img.commit()
    img.close()
    with open(path, "rb") as f:
        f.seek(512)
        cdh = f.read(512)
    assert cdh[3] & 0x40
    assert not cdh[3] & 0x80
    rc, out = cckdcdsk(path)
    assert rc == 0, out


def test_rollback_discards_write(fixture_images):
    path = fixture_images["cckd64"]
    raw_before = path.read_bytes()
    img = open_image(path, writable=True)
    orig = img.read_track(5, 3)
    img.begin()
    img.write_track(5, 3, build_track(5, 3, [(1, b"", b"R" * 300)]))
    img.rollback()
    assert img.read_track(5, 3) == orig
    img.begin()  # a fresh transaction after rollback works
    img.write_track(5, 4, build_track(5, 4, [(1, b"", b"S" * 300)]))
    img.commit()
    img.close()
    assert path.read_bytes() != raw_before
    rc, out = cckdcdsk(path)
    assert rc == 0, out


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dasdinit(tmp_path, tool, linux=False):
    path = tmp_path / "empty.cckd"
    args = [str(ORACLE_BIN / tool), "-z"] + (["-linux"] if linux else []) + [str(path), "3390-1", "EMPTY1", "40"]
    subprocess.run(args, check=True, capture_output=True, cwd=ORACLE_BIN)
    return path


@pytest.mark.parametrize("tool", ["dasdinit", "dasdinit64"])
def test_failed_write_poisons_transaction(tmp_path, tool, monkeypatch):
    """A write that fails after it has pended part of its changes (here: the
    track image is allocated, then its new L2 table does not fit) must not be
    committable; rollback() restores everything and the object stays usable."""
    from ckdoffline.image import cckdspace
    path = _dasdinit(tmp_path, tool)
    before = _sha(path)
    size, _foff, _num, _ = _free_blocks(path)
    img = open_image(path, writable=True)
    try:
        assert img._l1[1] == 0  # group 1 has no L2 table
        img.begin()
        monkeypatch.setattr(cckdspace, "max_file_size", lambda is64: size + 1000)
        t = build_track(20, 7, [(1, b"", b"p" * 100)])  # track 307, group 1
        with pytest.raises(NoSpaceError):
            img.write_track(20, 7, t)  # the image fits at the end, its L2 table does not
        monkeypatch.undo()
        with pytest.raises(CkdError, match="transaction aborted"):
            img.commit()
        with pytest.raises(CkdError, match="transaction aborted"):
            img.write_track(20, 8, t)
        assert img.raw.in_transaction  # still open: only rollback is accepted
        img.rollback()
        assert _sha(path) == before
        img.begin()
        img.write_track(20, 7, t)
        img.commit()
        assert img.read_track(20, 7) == t
    finally:
        img.close()
    rc, out = cckdcdsk(path)
    assert rc == 0, out
    check_space(path)


def test_failed_ckd_write_poisons_transaction(fixture_images, monkeypatch):
    path = fixture_images["ckd"]
    before = _sha(path)
    img = open_image(path, writable=True)
    try:
        img.begin()
        monkeypatch.setattr(img.raw, "write",
                            lambda off, data: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            img.write_track(5, 3, build_track(5, 3, [(1, b"", b"x")]))
        monkeypatch.undo()
        with pytest.raises(CkdError, match="transaction aborted: disk full"):
            img.commit()
        img.rollback()
        img.begin()
        img.write_track(5, 3, build_track(5, 3, [(1, b"", b"y")]))
        img.rollback()
    finally:
        img.close()
    assert _sha(path) == before


@pytest.mark.parametrize("key", ["ckd", "cckd_zlib", "cckd64"])
def test_write_outside_txn_raises(fixture_images, key):
    img = open_image(fixture_images[key], writable=True)
    try:
        t = build_track(5, 3, [(1, b"", b"x")])
        with pytest.raises(CkdError):
            img.write_track(5, 3, t)
        img.begin()
        img.write_track(5, 3, t)
        img.commit()
        with pytest.raises(CkdError):
            img.write_track(5, 3, t)  # the transaction has ended
    finally:
        img.close()
    ro = open_image(fixture_images[key], writable=False)
    try:
        with pytest.raises(CkdError):
            ro.write_track(5, 3, t)
    finally:
        ro.close()


@pytest.mark.parametrize("key", ["ckd", "cckd64"])
def test_write_rejects_bad_images(fixture_images, key):
    img = open_image(fixture_images[key], writable=True)
    try:
        img.begin()
        good = build_track(5, 3, [(1, b"", b"x")])
        bad = [
            build_track(5, 4, [(1, b"", b"x")]),              # wrong CCHH
            good[:-8],                                       # no end-of-track
            good + b"\x00",                                  # junk after it
            build_track(5, 3, [(1, b"", bytes(img.trksize))]),  # too long
            build_track(5, 3, [(0, b"", b"x")]),              # record 0 again
            b"\x01" + good[1:],                              # byte 0 not 0
        ]
        for t in bad:
            with pytest.raises(FormatError):
                img.write_track(5, 3, t)
        with pytest.raises(FormatError):
            img.write_track(100, 0, good)
        img.rollback()
    finally:
        img.close()


_MSG_PATH = re.compile(r"CCKD file \S+:")
