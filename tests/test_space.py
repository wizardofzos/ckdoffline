import hashlib

import pytest

from ckdoffline.errors import FormatError, NoSpaceError, UnsupportedError
from ckdoffline.image.base import open_image
from ckdoffline.vtoc import space
from ckdoffline.vtoc.dscb import Extent, F5Extent, Format1, Format3, Format4, Format5
from ckdoffline.vtoc.vtoc import Volume

from invariants import allocated_tracks
from oracle import cckdcdsk
from test_vtoc import dasdls_info

HEADS = 15
FIXTURE_TRACKS = 100 * HEADS
FIXTURE_FIRST_FREE = 21          # 0/0 track 0, 0/1-1/4 datasets, 1/5 the VTOC


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ranges(tracks):
    out = []
    for t in sorted(tracks):
        if out and out[-1][1] == t:
            out[-1][1] = t + 1
        else:
            out.append([t, t + 1])
    return [tuple(r) for r in out]


def _f5_tracks(vol):
    """Free tracks per the F5 chain, read independently of space.py."""
    e = vol.vtoc_extent
    ptr, out = (e.bcyl, e.bhead, 2), set()
    while any(ptr):
        f5 = vol.read_dscb(ptr)
        assert isinstance(f5, Format5)
        for x in f5.extents:
            out.update(range(x.btrk, x.btrk + x.ncyl * HEADS + x.ntrk))
        ptr = f5.ptrds
    return out


def _assert_no_overlap(vol):
    """Every extent on the volume (datasets, VTOC, track 0) is disjoint from
    every other one."""
    heads = vol.image.heads
    seen = {0: "track 0"}
    owners = [(d.dsname, e) for d in vol.datasets() for e in d.extents] + [("VTOC", vol.vtoc_extent)]
    for name, e in owners:
        for t in range(e.bcyl * heads + e.bhead, e.ecyl * heads + e.ehead + 1):
            assert t not in seen, f"track {t} is in {seen[t]} and {name}"
            seen[t] = name


def _extend(path, dsname, min_tracks=1, times=1):
    img = open_image(path, True)
    vol = Volume(img)
    out = []
    for _ in range(times):
        img.begin()
        try:
            out.append(space.extend(vol, dsname, min_tracks))
        except BaseException:
            img.rollback()
            raise
        img.commit()
    img.close()
    return out


def _set_scalo(path, dsname, scalo, scext=None):
    img = open_image(path, True)
    vol = Volume(img)
    c, f1 = vol.f1(dsname)
    f1.scalo = scalo
    if scext is not None:
        f1.raw[0x4F:0x52] = scext
    img.begin()
    vol.write_dscb(c, f1)
    img.commit()
    img.close()


# -- field offsets (dasdblks.h / IBM Advanced Services tables) -------------
def test_format1_written_field_offsets():
    f1 = Format1(bytes(140))
    f1.noepv = 0x5A
    f1.extents = [Extent(0x01, 0, 0x0102, 0x0304, 0x0506, 0x0708),
                  Extent(0x81, 1, 0x1112, 0x1314, 0x1516, 0x1718),
                  Extent(0x01, 2, 0x2122, 0x2324, 0x2526, 0x2728)]
    f1.ptrds = (0x3132, 0x3334, 0x35)
    want = bytearray(140)
    want[0x3B] = 0x5A
    want[0x69:0x73] = bytes.fromhex("0100 0102 0304 0506 0708")
    want[0x73:0x7D] = bytes.fromhex("8101 1112 1314 1516 1718")
    want[0x7D:0x87] = bytes.fromhex("0102 2122 2324 2526 2728")
    want[0x87:0x8C] = bytes.fromhex("3132333435")
    assert f1.raw == want


def test_format3_written_field_offsets():
    f3 = Format3.blank()
    want = bytearray(140)
    want[0:4] = b"\x03\x03\x03\x03"
    want[44] = 0xF3
    assert f3.raw == want and f3.keyid == b"\x03" * 4
    exts = [Extent(0x01, i, 0x100 + i, i, 0x200 + i, 14 - i) for i in range(13)]
    f3.extents = exts
    f3.ptrds = (0x0A0B, 0x0C0D, 0x0E)
    for i, e in enumerate(exts):
        off = 4 + 10 * i if i < 4 else 45 + 10 * (i - 4)
        want[off:off + 10] = e.pack()
    want[135:140] = bytes.fromhex("0A0B0C0D0E")
    assert f3.raw == want
    assert f3.extents == exts


def test_format4_dsrec_offset():
    f4 = Format4(bytes(140))
    f4.dsrec = 0x1234
    want = bytearray(140)
    want[50:52] = b"\x12\x34"                 # DS4DSREC: data offset 6
    assert f4.raw == want


def test_format5_written_field_offsets():
    f5 = Format5.blank()
    want = bytearray(140)
    want[0:4] = b"\x05\x05\x05\x05"
    want[44] = 0xF5
    assert f5.raw == want and f5.keyid == b"\x05" * 4
    exts = [F5Extent(0x0100 + i, 0x0200 + i, i + 1) for i in range(26)]
    f5.extents = exts
    f5.ptrds = (0x0102, 0x0304, 0x05)
    for i, x in enumerate(exts):
        off = 4 + 5 * i if i < 8 else 45 + 5 * (i - 8)
        want[off:off + 5] = bytes.fromhex(f"{x.btrk:04X}{x.ncyl:04X}{x.ntrk:02X}")
    want[135:140] = bytes.fromhex("0102030405")
    assert f5.raw == want
    assert f5.extents == exts
    with pytest.raises(ValueError):
        f5.extents = exts + [F5Extent(1, 0, 1)]
    f5.extents = exts[:2]                      # unused entries are zeroed
    assert f5.raw[14:44] == bytes(30) and f5.raw[45:135] == bytes(90)


# -- secondary quantity ----------------------------------------------------
def _f1(units, qty, blksize=0, keylen=0, scext=b"\0\0\0"):
    f1 = Format1(bytes(140))
    f1.scalo = (units, qty)
    f1.blksize, f1.keylen = blksize, keylen
    f1.raw[0x4F:0x52] = scext
    return f1


def test_secondary_tracks():
    assert space.secondary_tracks(_f1(0x80, 7), HEADS) == (7, False)
    assert space.secondary_tracks(_f1(0x88, 7), HEADS) == (7, False)        # CONTIG
    assert space.secondary_tracks(_f1(0xC0, 5), HEADS) == (75, True)
    # BLK: 27920 fits twice on a 3390 track, 3120 fifteen times
    assert space.secondary_tracks(_f1(0x40, 5, 27920), HEADS) == (3, False)
    assert space.secondary_tracks(_f1(0x40, 30, 3120), HEADS) == (2, False)
    assert space.secondary_tracks(_f1(0x40, 31, 3120), HEADS) == (3, False)
    assert space.secondary_tracks(_f1(0x41, 31, 3120), HEADS) == (15, True)   # ROUND
    # DS1EXT + DS1SCAVB: the requested block length is DS1SCXTV, not DS1BLKL
    assert space.secondary_tracks(_f1(0x50, 9, 3120, scext=b"\x80\x04\x00"), HEADS) == (1, False)
    assert space.secondary_tracks(_f1(0x50, 100, 3120, scext=b"\x80\x04\x00"), HEADS) == (4, False)  # 33/trk
    # TRK/CYL units ignore the extension
    assert space.secondary_tracks(_f1(0x90, 5, 3120, scext=b"\x18\x03\x62"), HEADS) == (5, False)
    with pytest.raises(NoSpaceError, match="no secondary allocation"):
        space.secondary_tracks(_f1(0x80, 0), HEADS)
    with pytest.raises(UnsupportedError, match="ABSTR"):
        space.secondary_tracks(_f1(0x00, 5), HEADS)
    with pytest.raises(UnsupportedError, match="average record"):
        space.secondary_tracks(_f1(0x50, 5, 3120, scext=b"\x20\x00\x50"), HEADS)
    with pytest.raises(UnsupportedError, match="block size"):
        space.secondary_tracks(_f1(0x40, 5, 0), HEADS)


# -- free space --------------------------------------------------------------
def test_free_extents_fixture(f5_image):
    vol = Volume(open_image(f5_image, False))
    assert space.free_extents(vol) == [(FIXTURE_FIRST_FREE, FIXTURE_TRACKS)]


def test_f5_out_of_sync_refused(f5_image):
    img = open_image(f5_image, True)
    vol = Volume(img)
    e = vol.vtoc_extent
    f5 = vol.read_dscb((e.bcyl, e.bhead, 2))
    f5.extents = [F5Extent(FIXTURE_FIRST_FREE + 1, 98, 13)]   # loses one free track
    img.begin()
    vol.write_dscb((e.bcyl, e.bhead, 2), f5)
    img.commit()
    img.begin()
    with pytest.raises(FormatError, match="format 5"):
        space.extend(vol, "TEST.FB.SEQ", 1)
    img.rollback()


# -- extend on a non-indexed volume (F5) --------------------------------------
def test_extend_updates_f5(f5_image):
    before = Volume(open_image(f5_image, False))
    dsrec = before.f4.dsrec
    [ext] = _extend(f5_image, "TEST.FB.SEQ", 1)
    assert ext == Extent(0x01, 1, 1, 6, 1, 6)
    vol = Volume(open_image(f5_image, False))
    assert vol.extents_of("TEST.FB.SEQ")[1] == ext
    assert vol.f1("TEST.FB.SEQ")[1].noepv == 2
    assert dasdls_info(f5_image)["TEST.FB.SEQ"]["extents"] == [(1, 1, 1, 2), (1, 6, 1, 6)]
    f5 = _f5_tracks(vol)
    assert FIXTURE_FIRST_FREE not in f5
    assert f5 == set(range(FIXTURE_TRACKS)) - allocated_tracks(vol)
    assert vol.read_dscb(vol.vtoc_cchhr).dsrec == dsrec           # no DSCB used
    _assert_no_overlap(vol)
    assert cckdcdsk(f5_image, level=3)[0] == 0


def test_extend_dos_bit_refused(fixture_images):
    path = fixture_images["ckd"]
    sha = _sha(path)
    with pytest.raises(UnsupportedError, match=r"free space map invalid \(DOS bit\)"):
        _extend(path, "TEST.FB.SEQ", 1)
    assert _sha(path) == sha


@pytest.mark.parametrize("bit", [0x04, 0x40])
def test_extend_dirf_refused(f5_image, bit):
    img = open_image(f5_image, True)
    vol = Volume(img)
    f4 = vol.read_dscb(vol.vtoc_cchhr)
    f4.vtoci |= bit
    img.begin()
    vol.write_dscb(vol.vtoc_cchhr, f4)
    img.commit()
    img.close()
    sha = _sha(f5_image)
    with pytest.raises(UnsupportedError, match="DIRF"):
        _extend(f5_image, "TEST.FB.SEQ", 1)
    assert _sha(f5_image) == sha


def test_extend_cylinder_aligned(f5_image):
    _set_scalo(f5_image, "TEST.FB.SEQ", (0xC0, 1))
    [ext] = _extend(f5_image, "TEST.FB.SEQ", 1)
    assert ext == Extent(0x81, 1, 2, 0, 2, 14)
    vol = Volume(open_image(f5_image, False))
    assert _ranges(_f5_tracks(vol)) == [(FIXTURE_FIRST_FREE, 30), (45, FIXTURE_TRACKS)]
    # min_tracks beyond the secondary is rounded up to whole cylinders
    [ext] = _extend(f5_image, "TEST.FB.SEQ", 16)
    assert ext == Extent(0x81, 2, 3, 0, 4, 14)
    # a track request takes the gap left below the aligned extent
    [ext] = _extend(f5_image, "TEST.VB.SEQ", 2)
    assert ext == Extent(0x01, 1, 1, 6, 1, 7)
    vol = Volume(open_image(f5_image, False))
    assert _f5_tracks(vol) == set(range(FIXTURE_TRACKS)) - allocated_tracks(vol)
    _assert_no_overlap(vol)


def test_extend_needs_f3(f5_image):
    vol0 = Volume(open_image(f5_image, False))
    dsrec, slot = vol0.f4.dsrec, min(vol0.free_dscb_slots())
    exts = _extend(f5_image, "TEST.FB.SEQ", 1, times=3)
    vol = Volume(open_image(f5_image, False))
    c, f1 = vol.f1("TEST.FB.SEQ")
    assert f1.noepv == 4 and len(f1.extents) == 3
    assert f1.ptrds == slot
    f3 = vol.read_dscb(slot)
    assert isinstance(f3, Format3) and f3.keyid == b"\x03" * 4
    assert f3.extents == [exts[2]] and not any(f3.ptrds)
    assert [e.seq for e in vol.extents_of("TEST.FB.SEQ")] == [0, 1, 2, 3]
    assert vol.read_dscb(vol.vtoc_cchhr).dsrec == dsrec - 1
    assert slot not in vol.free_dscb_slots()
    ls = dasdls_info(f5_image)["TEST.FB.SEQ"]["extents"]
    assert ls == [(e.bcyl, e.bhead, e.ecyl, e.ehead) for e in vol.extents_of("TEST.FB.SEQ")]
    assert len(ls) == 4
    assert _f5_tracks(vol) == set(range(FIXTURE_TRACKS)) - allocated_tracks(vol)
    _assert_no_overlap(vol)
    assert cckdcdsk(f5_image, level=3)[0] == 0


def test_16_extent_limit(f5_image):
    _extend(f5_image, "TEST.FB.SEQ", 1, times=15)
    vol = Volume(open_image(f5_image, False))
    exts = vol.extents_of("TEST.FB.SEQ")
    assert len(exts) == 16 and vol.f1("TEST.FB.SEQ")[1].noepv == 16
    assert len(vol.read_dscb(vol.f1("TEST.FB.SEQ")[1].ptrds).extents) == 13
    ls = dasdls_info(f5_image)["TEST.FB.SEQ"]["extents"]
    assert ls == [(e.bcyl, e.bhead, e.ecyl, e.ehead) for e in exts]
    _assert_no_overlap(vol)
    sha = _sha(f5_image)
    with pytest.raises(NoSpaceError, match="16 extents"):
        _extend(f5_image, "TEST.FB.SEQ", 1)
    assert _sha(f5_image) == sha
    assert cckdcdsk(f5_image, level=3)[0] == 0


def test_volume_full(f5_image):
    rest = FIXTURE_TRACKS - FIXTURE_FIRST_FREE
    sha = _sha(f5_image)
    with pytest.raises(NoSpaceError, match="volume full"):
        _extend(f5_image, "TEST.VB.SEQ", rest + 1)
    assert _sha(f5_image) == sha
    [ext] = _extend(f5_image, "TEST.VB.SEQ", rest)
    assert ext.tracks(HEADS) == rest
    vol = Volume(open_image(f5_image, False))
    assert space.free_extents(vol) == [] and _f5_tracks(vol) == set()
    e = vol.vtoc_extent
    assert vol.read_dscb((e.bcyl, e.bhead, 2)).extents == []      # the first F5 stays
    sha = _sha(f5_image)
    with pytest.raises(NoSpaceError, match="volume full"):
        _extend(f5_image, "TEST.FB.SEQ", 1)
    assert _sha(f5_image) == sha
    _assert_no_overlap(vol)


def test_extend_refuses_non_editable(f5_image):
    img = open_image(f5_image, True)
    vol = Volume(img)
    c, f1 = vol.f1("TEST.FB.SEQ")
    f1.smsfg |= 0x08                            # PDSE
    img.begin()
    vol.write_dscb(c, f1)
    img.commit()
    img.begin()
    with pytest.raises(UnsupportedError, match="PDSE"):
        space.extend(vol, "TEST.FB.SEQ", 1)
    img.rollback()


def test_f5_chain_grows_and_shrinks(f5_image):
    """More than 26 free ranges need a second F5 from a free DSCB slot; fewer
    ranges free it again. DS4DSREC follows on a non-indexed VTOC."""
    img = open_image(f5_image, True)
    vol = Volume(img)
    dsrec, slot = vol.f4.dsrec, min(vol.free_dscb_slots())
    e = vol.vtoc_extent
    first = (e.bcyl, e.bhead, 2)
    many = [(100 + 20 * i, 100 + 20 * i + 3) for i in range(30)]
    img.begin()
    space._write_f5_chain(vol, many)
    img.commit()
    f5 = vol.read_dscb(first)
    assert len(f5.extents) == 26 and f5.ptrds == slot
    f5b = vol.read_dscb(slot)
    assert isinstance(f5b, Format5) and f5b.keyid == b"\x05" * 4 and not any(f5b.ptrds)
    assert _ranges(_f5_tracks(vol)) == many
    assert vol.read_dscb(vol.vtoc_cchhr).dsrec == dsrec - 1
    img.begin()
    space._write_f5_chain(vol, many[:3])
    img.commit()
    f5 = vol.read_dscb(first)
    assert len(f5.extents) == 3 and not any(f5.ptrds)
    assert slot in vol.free_dscb_slots()
    assert vol.read_dscb(vol.vtoc_cchhr).dsrec == dsrec
