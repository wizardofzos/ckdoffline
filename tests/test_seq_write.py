"""Task 12: writing sequential datasets (DatasetIO.write_blocks, write_sequential).

PS end of data follows z/OS (checked on 38 real PS datasets): DS1LSTAR is the last
data block, DS1TRBAL is the balance of the data records on that track, and the
EOF is R1 of the next track (none when the data ends on the last allocated
track). An empty PS dataset has DS1LSTAR = 0 and an EOF as R1 of track 0.
PO (Task 13) keeps the EOF after the last block and points DS1LSTAR at it.
"""

import datetime
import hashlib

import pytest

from ckdoffline.dataset.extents import DatasetIO
from ckdoffline.dataset.sequential import read_sequential, write_sequential
from ckdoffline.errors import CkdError, FormatError, NoSpaceError, ValidationError
from ckdoffline.image.base import open_image
from ckdoffline.track import Record, build_track, is_eof, parse_track, track_balance
from ckdoffline.vtoc import space
from ckdoffline.vtoc.vtoc import Volume

FIXTURE_TRACKS = 100 * 15   # tracks on the synthetic 100-cylinder test volume
from invariants import allocated_tracks
from oracle import cckdcdsk, dasdseq
from test_space import _f5_tracks
from test_vtoc import dasdls_info

FULL_TRACK = 58786


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _vol(path, writable=False):
    return Volume(open_image(path, writable))


def _write(path, dsname, records):
    img = open_image(path, True)
    vol = Volume(img)
    img.begin()
    try:
        write_sequential(vol, dsname, records)
    except BaseException:
        img.rollback()
        img.close()
        raise
    img.commit()
    img.close()


def _shape(io, tt):
    return [(r.rec, len(r.key), len(r.data)) for r in io.read_rel_track(tt)]


def _today():
    t = datetime.date.today()
    return t.year - 1900, t.timetuple().tm_yday


def _fb(n, tag="L", lrecl=80):
    return [f"{tag}{i:06d} {'X' * (i % 50)}".ljust(lrecl).encode("cp037") for i in range(n)]


def _assert_f1_touched(f1):
    assert f1.dsind & 0x02
    assert f1.refdt == _today()


# -- rewrite / shrink / grow -------------------------------------------------------
@pytest.mark.parametrize("key", ["ckd", "cckd_zlib", "cckd_bz2", "cckd64"])
def test_seq_rewrite_same_size(fixture_images, key):
    path = fixture_images[key]
    recs = read_sequential(_vol(path), "TEST.FB.SEQ")
    recs[7] = "CHANGED LINE".ljust(80).encode("cp037")
    _write(path, "TEST.FB.SEQ", recs)
    vol = _vol(path)
    assert read_sequential(vol, "TEST.FB.SEQ") == recs
    assert dasdseq(path, "TEST.FB.SEQ") == b"".join(recs)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    # 500 x 80 in 3120-byte blocks: 12 full + one of 2560, all on track 0;
    # the EOF is R1 of track 1
    assert io.f1.lstar == (0, 13)
    assert io.f1.trbal == track_balance([(0, 3120)] * 12 + [(0, 2560)])
    assert _shape(io, 0) == [(r, 0, 3120) for r in range(1, 13)] + [(13, 0, 2560)]
    assert _shape(io, 1) == [(1, 0, 0)]
    _assert_f1_touched(io.f1)
    assert len(dasdls_info(path)["TEST.FB.SEQ"]["extents"]) == 1
    assert cckdcdsk(path, level=3)[0] == 0


def test_seq_grow_into_secondary(f5_image):
    """TEST.FB.SEQ: 2 tracks, secondary 1 track. 2000 records = 52 blocks of
    3120 at 15 per track = 4 tracks: two secondary extents; the data ends on
    the last allocated track, so there is no EOF (as z/OS does)."""
    recs = _fb(2000)
    _write(f5_image, "TEST.FB.SEQ", recs)
    vol = _vol(f5_image)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    assert read_sequential(vol, "TEST.FB.SEQ") == recs
    assert dasdseq(f5_image, "TEST.FB.SEQ") == b"".join(recs)
    assert len(io.extents) == 3 and io.total_tracks == 4
    assert io.f1.lstar == (3, 7)
    assert io.f1.trbal == track_balance([(0, 3120)] * 6 + [(0, 880)])
    assert [len(io.read_rel_track(t)) for t in range(4)] == [15, 15, 15, 7]
    assert not any(is_eof(r) for t in range(4) for r in io.read_rel_track(t))
    _assert_f1_touched(io.f1)
    assert len(dasdls_info(f5_image)["TEST.FB.SEQ"]["extents"]) == 3
    assert _f5_tracks(vol) == set(range(FIXTURE_TRACKS)) - allocated_tracks(vol)
    assert cckdcdsk(f5_image, level=3)[0] == 0


# -- the end-of-data layout ---------------------------------------------------------------


# -- content ---------------------------------------------------------------------------


def test_untouched_unmappable_bytes_roundtrip(fixture_images):
    path = fixture_images["cckd_zlib"]
    odd = [bytes([0x00, 0x05, 0xFF, 0x40]) * 20,
           b"\x00" * 80,
           b"\xff" * 80,
           bytes(range(80)),
           bytes(range(176, 256)),
           b"\x05" + "TAB".ljust(78).encode("cp037") + b"\x00"]
    fb = _fb(10) + odd + _fb(5, "T")
    _write(path, "TEST.FB.SEQ", fb)
    assert read_sequential(_vol(path), "TEST.FB.SEQ") == fb
    assert dasdseq(path, "TEST.FB.SEQ") == b"".join(fb)

    # VB: dasdseq refuses V records, so check the blocks against a hand-built
    # BDW/RDW layout (3120-byte blocks, greedy packing)
    vb = [b"\x00\x05\xff", b"", b"\xff" * 251, bytes(range(200)), b"\x05" * 250] * 7
    _write(path, "TEST.VB.SEQ", vb)
    vol = _vol(path)
    assert read_sequential(vol, "TEST.VB.SEQ") == vb
    want, cur = [], []
    for r in vb:
        if cur and 4 + sum(len(x) + 4 for x in cur) + len(r) + 4 > 3120:
            want.append(cur)
            cur = []
        cur.append(r)
    want.append(cur)
    expect = []
    for blk in want:
        body = b"".join((len(r) + 4).to_bytes(2, "big") + b"\0\0" + r for r in blk)
        expect.append((len(body) + 4).to_bytes(2, "big") + b"\0\0" + body)
    io = DatasetIO(vol, "TEST.VB.SEQ")
    assert [r.data for _, r in io.blocks()] == expect
    assert cckdcdsk(path, level=3)[0] == 0


def test_overlength_line_rejected_image_unchanged(fixture_images):
    path = fixture_images["ckd"]
    sha = _sha(path)
    recs = _fb(5)
    recs[2] = b"\x40" * 81
    with pytest.raises(ValidationError, match="line 3") as e:
        _write(path, "TEST.FB.SEQ", recs)
    assert e.value.line_no == 3
    with pytest.raises(ValidationError, match="line 2") as e:
        _write(path, "TEST.VB.SEQ", [b"ok", b"x" * 252])
    assert e.value.line_no == 2
    assert _sha(path) == sha
    assert len(read_sequential(_vol(path), "TEST.FB.SEQ")) == 500


# -- empty ----------------------------------------------------------------------------
def test_save_zero_records(fixture_images):
    path = fixture_images["cckd64"]
    _write(path, "TEST.FB.SEQ", [])
    vol = _vol(path)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    assert io.f1.lstar == (0, 0)
    assert io.f1.trbal == FULL_TRACK == track_balance([])
    r0, recs = parse_track(vol.image.read_track(*io.rel_to_cchh(0)))
    assert len(recs) == 1 and recs[0].rec == 1 and is_eof(recs[0])
    assert read_sequential(vol, "TEST.FB.SEQ") == []
    assert list(io.blocks()) == []
    _assert_f1_touched(io.f1)
    assert cckdcdsk(path, level=3)[0] == 0
    # and back from empty
    _write(path, "TEST.FB.SEQ", _fb(3))
    assert read_sequential(_vol(path), "TEST.FB.SEQ") == _fb(3)


def test_read_never_written_dataset(fixture_images):
    img = open_image(fixture_images["ckd"], True)
    vol = Volume(img)
    c, f1 = vol.f1("TEST.VB.SEQ")
    f1.lstar = (0, 0)
    io = DatasetIO(vol, "TEST.VB.SEQ")
    cyl, head = io.rel_to_cchh(0)
    junk = [Record(cyl, head, i, b"", bytes([0xC0 + i]) * 300) for i in range(1, 4)]
    img.begin()
    vol.write_dscb(c, f1)
    img.write_track(cyl, head, build_track(cyl, head, junk))
    img.commit()
    assert read_sequential(vol, "TEST.VB.SEQ") == []
    assert list(DatasetIO(vol, "TEST.VB.SEQ").blocks()) == []


# -- write_blocks: the PO ending (used by Task 13) ---------------------------------------
def _write_blocks(path, dsname, start, blocks, ending):
    img = open_image(path, True)
    vol = Volume(img)
    img.begin()
    try:
        ttr = DatasetIO(vol, dsname).write_blocks(start, blocks, ending=ending)
    except BaseException:
        img.rollback()
        img.close()
        raise
    img.commit()
    img.close()
    return ttr


def test_write_blocks_po_matches_dasdload(fixture_images):
    """dasdload writes PS datasets PO style (EOF inline, DS1LSTAR = EOF), so
    the PO ending reproduces its track exactly."""
    path = fixture_images["ckd"]
    vol = _vol(path)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    track0 = vol.image.read_track(*io.rel_to_cchh(0))
    lstar, trbal = io.f1.lstar, io.f1.trbal
    blocks = [r.data for _, r in io.blocks()]
    assert _write_blocks(path, "TEST.FB.SEQ", (0, 1), blocks, "PO") == lstar == (0, 14)
    vol = _vol(path)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    assert (io.f1.lstar, io.f1.trbal) == (lstar, trbal)
    assert vol.image.read_track(*io.rel_to_cchh(0)) == track0


def test_write_blocks_po_keeps_records_before_start(fixture_images):
    path = fixture_images["ckd"]
    vol = _vol(path)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    keep = [r.data for r in io.read_rel_track(0)[:4]]
    ttr = _write_blocks(path, "TEST.FB.SEQ", (0, 5), [b"\xc1" * 100, b"\xc2" * 200], "PO")
    assert ttr == (0, 7)
    io = DatasetIO(_vol(path), "TEST.FB.SEQ")
    recs = io.read_rel_track(0)
    assert [r.data for r in recs[:4]] == keep
    assert [len(r.data) for r in recs[4:]] == [100, 200, 0]
    assert io.f1.trbal == track_balance([(0, 3120)] * 4 + [(0, 100), (0, 200), (0, 0)])


def test_write_blocks_po_eof_needs_next_track_and_extends(f5_image):
    """30 blocks of 3120 fill both tracks exactly (15 per track, the EOF does
    not fit after 15): PO must extend to get a track for the EOF."""
    blocks = [bytes([i]) * 3120 for i in range(1, 31)]
    ttr = _write_blocks(f5_image, "TEST.FB.SEQ", (0, 1), blocks, "PO")
    assert ttr == (2, 1)
    vol = _vol(f5_image)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    assert len(io.extents) == 2
    assert _shape(io, 2) == [(1, 0, 0)]
    assert io.f1.lstar == (2, 1) and io.f1.trbal == track_balance([(0, 0)])
    assert [r.data for _, r in io.blocks()] == blocks
    assert cckdcdsk(f5_image, level=3)[0] == 0


def test_write_blocks_ps_full_tracks_no_extend_for_eof(fixture_images):
    """The same 30 blocks as PS: the data ends on the last allocated track,
    so there is no EOF and no extend (the DOS-bit fixture cannot extend)."""
    path = fixture_images["ckd"]
    blocks = [bytes([i]) * 3120 for i in range(1, 31)]
    assert _write_blocks(path, "TEST.FB.SEQ", (0, 1), blocks, "PS") == (1, 15)
    io = DatasetIO(_vol(path), "TEST.FB.SEQ")
    assert io.f1.trbal == track_balance([(0, 3120)] * 15)
    assert [r.data for _, r in io.blocks()] == blocks


def test_write_blocks_rejects_bad_blocks(fixture_images):
    path = fixture_images["ckd"]
    sha = _sha(path)
    with pytest.raises(ValidationError, match="empty"):
        _write_blocks(path, "TEST.FB.SEQ", (0, 1), [b"x", b""], "PS")
    with pytest.raises(ValidationError, match="track"):
        _write_blocks(path, "TEST.FB.SEQ", (0, 1), [b"x" * 60000], "PS")
    with pytest.raises(ValueError):
        _write_blocks(path, "TEST.FB.SEQ", (0, 1), [b"x"], "DA")
    assert _sha(path) == sha


# -- failure while growing ----------------------------------------------------------------
def test_grow_past_16_extents_refused_before_staging(f5_image):
    """TEST.FB.SEQ: 2 tracks + at most 15 one-track secondaries = 17 tracks;
    23400 records need 40. Refused up front; nothing is staged."""
    sha = _sha(f5_image)
    img = open_image(f5_image, True)
    vol = Volume(img)
    img.begin()
    with pytest.raises(NoSpaceError, match="16 at most"):
        write_sequential(vol, "TEST.FB.SEQ", _fb(23400))
    assert len(DatasetIO(vol, "TEST.FB.SEQ").extents) == 1
    img.rollback()
    img.close()
    assert _sha(f5_image) == sha
    _write(f5_image, "TEST.FB.SEQ", _fb(3))
    assert read_sequential(_vol(f5_image), "TEST.FB.SEQ") == _fb(3)
    assert cckdcdsk(f5_image, level=3)[0] == 0


def test_volume_full_refused_before_staging(f5_image):
    _set_secondary(f5_image, "TEST.FB.SEQ", (0x80, 2000))      # 2000 tracks > the volume
    sha = _sha(f5_image)
    img = open_image(f5_image, True)
    vol = Volume(img)
    img.begin()
    with pytest.raises(NoSpaceError, match="volume full"):
        write_sequential(vol, "TEST.FB.SEQ", _fb(2000))
    img.rollback()
    img.close()
    assert _sha(f5_image) == sha


def _set_secondary(path, dsname, scalo):
    img = open_image(path, True)
    vol = Volume(img)
    c, f1 = vol.f1(dsname)
    f1.scalo = scalo
    img.begin()
    vol.write_dscb(c, f1)
    img.commit()
    img.close()


def test_extend_failure_after_staging_poisons(f5_image, monkeypatch):
    """The pre-check passes but the third extend fails (as it can when free
    space is too fragmented): two extents are already staged, so commit()
    must refuse; rollback restores the image exactly."""
    sha = _sha(f5_image)
    real, calls = space.extend, []

    def flaky(volume, dsname, min_tracks):
        calls.append(dsname)
        if len(calls) == 3:
            raise NoSpaceError("volume full: simulated")
        return real(volume, dsname, min_tracks)
    monkeypatch.setattr(space, "extend", flaky)
    img = open_image(f5_image, True)
    vol = Volume(img)
    img.begin()
    with pytest.raises(NoSpaceError, match="simulated"):
        write_sequential(vol, "TEST.FB.SEQ", _fb(3000))       # 77 blocks: 6 tracks, 4 extends
    assert len(calls) == 3
    with pytest.raises(CkdError, match="transaction aborted"):
        img.commit()
    img.rollback()
    img.close()
    monkeypatch.undo()
    assert _sha(f5_image) == sha
    _write(f5_image, "TEST.FB.SEQ", _fb(10))
    assert read_sequential(_vol(f5_image), "TEST.FB.SEQ") == _fb(10)
    assert cckdcdsk(f5_image, level=3)[0] == 0


def test_unreadable_track_is_reported(fixture_images, monkeypatch):
    path = fixture_images["cckd_zlib"]
    img = open_image(path, True)
    vol = Volume(img)
    io = DatasetIO(vol, "TEST.FB.SEQ")
    bad = io.rel_to_cchh(1)
    real = type(img).read_track

    def read_track(self, cyl, head):
        if (cyl, head) == bad:
            raise FormatError("track 21: zlib error: simulated")
        return real(self, cyl, head)
    monkeypatch.setattr(type(img), "read_track", read_track)
    img.begin()
    with pytest.raises(FormatError, match=r"cannot be read before it is rewritten.*zlib"):
        write_sequential(vol, "TEST.FB.SEQ", _fb(10))
    with pytest.raises(CkdError, match="transaction aborted"):
        img.commit()
    img.rollback()
