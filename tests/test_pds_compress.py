"""Task 14: in-place PDS compress (like IEBCOPY compress).

Layout after compress: the directory blocks and their EOF stay put; every distinct member (by
TTR, in TTR order; aliases share one copy) follows, block for block, each with its own EOF;
DS1LSTAR is the last member's EOF. Directory TTRs, the TTRN fields in user data and the TTRs in
note lists are relocated; everything else is byte-identical.
"""

import hashlib

import pytest

from ckdoffline import ebcdic
from ckdoffline.dataset.pds import Pds
from ckdoffline.errors import FormatError, UnsupportedError
from ckdoffline.image.base import open_image
from ckdoffline.vtoc.vtoc import Volume

from oracle import cckdcdsk, dasdcat

FB = "TEST.FB80.PDS"
ULIB = "TEST.LOAD"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _txn(path, dsn, fn):
    img = open_image(path, True)
    pds = Pds(Volume(img), dsn)
    img.begin()
    try:
        out = fn(pds)
    except BaseException:
        img.rollback()
        img.close()
        raise
    img.commit()
    img.close()
    return out


def _pds(path, dsn):
    return Pds(Volume(open_image(path, False)), dsn)


def _text(lines, lrecl=80):
    return [l.ljust(lrecl).encode("cp037") for l in lines]


def _name(n):
    return ebcdic.from_text(n.ljust(8), "1047")


def _ttr3(ttr):
    return ttr[0].to_bytes(2, "big") + bytes([ttr[1]])


def _snapshot(pds):
    """{name: (TTR, C-byte-less raw tail, [block bytes], [TTRN -> (block index, N)])}."""
    out = {}
    for e in pds.directory():
        blocks = pds.read_member_blocks(e.name)
        index = {t: i for i, (t, _) in enumerate(blocks)}
        ttrns = []
        for k in range(e.ttrn_count):
            f = e.userdata[4 * k:4 * k + 4]
            t = (int.from_bytes(f[:2], "big"), f[2])
            ttrns.append((None if t == (0, 0) else index[t], f[3]))      # TTR 0: no pointer
        out[e.name] = (e.ttr, e.alias, e.userdata[4 * e.ttrn_count:], [b for _, b in blocks], ttrns)
    return out


def _assert_same_members(before, after):
    assert before.keys() == after.keys()
    for name, (_, alias, tail, blocks, ttrns) in before.items():
        _, alias2, tail2, blocks2, ttrns2 = after[name]
        assert (alias2, tail2, blocks2, ttrns2) == (alias, tail, blocks, ttrns), name
    # entries that shared data before still share it (aliases), and only they do
    groups = lambda s: sorted(sorted(n for n in s if s[n][0] == t) for t in {v[0] for v in s.values()})
    assert groups(after) == groups(before)


# -- fixture ----------------------------------------------------------------------------------
def test_compress_after_replaces(fixture_images):
    path = fixture_images["cckd_zlib"]
    other = _text([f"OTHER {i}" for i in range(30)])
    _txn(path, FB, lambda p: p.save_member("FIRST", other, "U"))
    for n in range(20):
        _txn(path, FB, lambda p: p.save_member(
            "HOT", _text([f"VERSION {n} LINE {i}" for i in range(100)]), "U"))
    _txn(path, FB, lambda p: p.save_member("LAST", _text(["LAST ONE"]), "U"))
    pds = _pds(path, FB)
    before = _snapshot(pds)
    lstar0 = pds.io.end_of_data()
    stats0 = {e.name: e.userdata for e in pds.directory()}

    result = _txn(path, FB, lambda p: p.compress())

    pds = _pds(path, FB)
    _assert_same_members(before, _snapshot(pds))
    assert {e.name: e.userdata for e in pds.directory()} == stats0
    lstar = pds.io.end_of_data()
    assert lstar[0] < lstar0[0]
    assert result == {"tracks_before": lstar0[0] + 1, "tracks_after": lstar[0] + 1, "members": 3}
    # directory EOF at R4 of track 0, the first member right after it, members back to back
    d = pds.directory()
    ttrs = sorted(e.ttr for e in d)
    assert ttrs[0] == (0, 5)
    recs = [(tt, r.rec) for tt in range(lstar[0] + 1) for r in pds.io.read_rel_track(tt)
            if not r.data and not r.key]
    assert recs[-1] == lstar and len(recs) == 4          # directory EOF + one EOF per member
    assert pds.read_member("HOT") == _text([f"VERSION 19 LINE {i}" for i in range(100)])
    for e in d:
        assert dasdcat(path, FB, e.name, ascii=False) == b"".join(before[e.name][3])
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_compress_already_compact_is_noop(fixture_images):
    path = fixture_images["cckd_zlib"]
    for n in range(5):
        _txn(path, FB, lambda p: p.save_member("HOT", _text([f"V{n}"] * 200), "U"))
    _txn(path, FB, lambda p: p.compress())
    sha = _sha(path)
    result = _txn(path, FB, lambda p: p.compress())
    assert _sha(path) == sha
    assert result["tracks_before"] == result["tracks_after"]


def test_compress_empty_pds_is_noop(fixture_images):
    path = fixture_images["cckd_zlib"]
    sha = _sha(path)
    assert _txn(path, FB, lambda p: p.compress()) == {"tracks_before": 1, "tracks_after": 1,
                                                       "members": 0}
    assert _sha(path) == sha


def test_compress_after_deleting_every_member(fixture_images):
    """Only dead data left: DS1LSTAR goes back to the directory's EOF."""
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("GONE", _text(["X"] * 400), "U"))
    _txn(path, FB, lambda p: p.delete_member("GONE"))
    assert _pds(path, FB).io.end_of_data() != (0, 4)
    assert _txn(path, FB, lambda p: p.compress())["members"] == 0
    pds = _pds(path, FB)
    assert pds.io.end_of_data() == (0, 4) and pds.directory() == []
    assert [r.rec for r in pds.io.read_rel_track(0)] == [1, 2, 3, 4]
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_compress_aliases_zero_ttr_and_empty_member(fixture_images):
    """An alias of a replaced member keeps its own (old) copy; aliases sharing a TTR share one
    copy; a TTR 0 entry stays 0; a zero-record member (TTR = its EOF) gets a new EOF."""
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("MAIN", _text(["OLD"]), "U"))

    def add_entries(p):
        d = p._read_dir()
        main = d.raws[0]
        extra = [_name("ALIAS1") + main[8:11] + b"\x80", _name("ALIAS2") + main[8:11] + b"\x80",
                 _name("NODATA") + b"\0\0\0\0"]
        p._stage_directory(d, p._plan_directory(d, d.raws + extra))
    _txn(path, FB, add_entries)
    _txn(path, FB, lambda p: p.save_member("JUNK", _text(["X"] * 500), "U"))
    _txn(path, FB, lambda p: p.delete_member("JUNK"))
    _txn(path, FB, lambda p: p.save_member("MAIN", _text(["NEW"]), "U"))
    _txn(path, FB, lambda p: p.save_member("EMPTY", [], "U"))
    pds = _pds(path, FB)
    before = _snapshot(pds)

    result = _txn(path, FB, lambda p: p.compress())

    pds = _pds(path, FB)
    _assert_same_members(before, _snapshot(pds))
    assert result["members"] == 5
    assert pds.read_member("ALIAS1") == pds.read_member("ALIAS2") == _text(["OLD"])
    assert pds.read_member("MAIN") == _text(["NEW"])
    assert pds._find("NODATA").ttr == (0, 0)
    # old copy (aliases), MAIN, EMPTY: in TTR order after the directory EOF (R4)
    assert pds._find("ALIAS1").ttr == (0, 5)
    assert pds._find("MAIN").ttr == (0, 7)
    empty = pds._find("EMPTY").ttr
    assert empty == (0, 9) == pds.io.end_of_data() and pds.read_member("EMPTY") == []
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def _add_u(p, name, blocks, userdata_fn, ttrn_count, alias_of=None):
    """Write U blocks after the end of data (bypassing save_member's RECFM U refusal) and add a
    directory entry whose user data userdata_fn(block TTRs) builds."""
    start = p._append_start()
    first = p.io.first_ttr(start, blocks, "PO")
    p.io.write_blocks(start, blocks, ending="PO")
    ttrs = [t for t, _ in p._blocks_at(first)]
    ud = userdata_fn(ttrs)
    d = p._read_dir()
    raws = [_name(name) + _ttr3(first) + bytes([ttrn_count << 5 | len(ud) // 2]) + ud]
    if alias_of:
        raws.append(_name(alias_of) + _ttr3(first) + bytes([0x80 | ttrn_count << 5 | len(ud) // 2]) + ud)
    p._stage_directory(d, p._plan_directory(d, d.raws + raws))
    return ttrs


def _load_module(p, name, alias=None):
    """Block 0 'ESD', block 1 a note list of the TTRs of blocks 2 and 3 (text). User data: TTRN 1
    = first text block (N 0), TTRN 2 = note list (N 2), then 4 attribute bytes."""
    def ud(ttrs):
        return (_ttr3(ttrs[2]) + b"\0" + _ttr3(ttrs[1]) + b"\x02" + b"\xc3\xf2\x00\x06")
    # the note list holds the TTRs of blocks 2-3: plan with a same-length placeholder first
    blocks = [b"\x20" * 40, bytes(12), b"\x01" * 1000, b"\x0d" * 500]
    start = p._append_start()
    first = p.io.first_ttr(start, blocks, "PO")
    nl = _ttr3((first[0], first[1] + 2)) + b"\0" + _ttr3((first[0], first[1] + 3)) + b"\0" + b"\xee" * 4
    blocks[1] = nl
    ttrs = _add_u(p, name, blocks, ud, 2, alias)
    assert ttrs[2] == (first[0], first[1] + 2) and ttrs[3] == (first[0], first[1] + 3)


def test_compress_relocates_ttrn_and_note_list(fixture_images):
    path = fixture_images["cckd_zlib"]
    _txn(path, ULIB, lambda p: _add_u(p, "DEAD", [b"\x99" * 5000] * 3, lambda t: b"", 0))
    _txn(path, ULIB, lambda p: p.delete_member("DEAD"))
    _txn(path, ULIB, lambda p: _load_module(p, "PGM", alias="PGMA"))
    pds = _pds(path, ULIB)
    old = pds._find("PGM")
    before = {n: pds.read_member_blocks(n) for n in ("PGM", "PGMA")}

    _txn(path, ULIB, lambda p: p.compress())

    pds = _pds(path, ULIB)
    e, a = pds._find("PGM"), pds._find("PGMA")
    assert e.ttr == a.ttr == (0, 5) != old.ttr
    assert a.alias and a.userdata == e.userdata
    blocks = pds.read_member_blocks("PGM")
    ttrs = [t for t, _ in blocks]
    assert ttrs == [(0, 5), (0, 6), (0, 7), (0, 8)]
    ud = e.userdata
    assert ud == _ttr3(ttrs[2]) + b"\0" + _ttr3(ttrs[1]) + b"\x02" + old.userdata[8:]
    assert blocks[1][1] == _ttr3(ttrs[2]) + b"\0" + _ttr3(ttrs[3]) + b"\0" + b"\xee" * 4
    old_blocks = before["PGM"]
    assert [b for _, b in blocks][0::2] == [b for _, b in old_blocks][0::2]
    assert blocks[3][1] == old_blocks[3][1]
    assert dasdcat(path, ULIB, "PGMA", ascii=False) == b"".join(b for _, b in blocks)
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_compress_refuses_dangling_ttrn(fixture_images):
    """A TTRN that points outside every member's data (here: a deleted member's block) cannot be
    relocated: refused before anything is staged."""
    path = fixture_images["cckd_zlib"]
    dead = _txn(path, ULIB, lambda p: _add_u(p, "DEAD", [b"\x99" * 100] * 2, lambda t: b"", 0))
    _txn(path, ULIB, lambda p: p.delete_member("DEAD"))
    _txn(path, ULIB, lambda p: _add_u(p, "PGM", [b"\x01" * 100] * 2,
                                      lambda t: _ttr3(dead[1]) + b"\0", 1))
    sha = _sha(path)
    with pytest.raises(FormatError, match="PGM"):
        _txn(path, ULIB, lambda p: p.compress())
    assert _sha(path) == sha


def test_compress_refuses_dangling_note_list_entry(fixture_images):
    path = fixture_images["cckd_zlib"]
    dead = _txn(path, ULIB, lambda p: _add_u(p, "DEAD", [b"\x99" * 100] * 2, lambda t: b"", 0))
    _txn(path, ULIB, lambda p: p.delete_member("DEAD"))
    _txn(path, ULIB, lambda p: _add_u(
        p, "PGM", [b"\x01" * 100, _ttr3(dead[0]) + b"\0", b"\x02" * 100],
        lambda t: _ttr3(t[2]) + b"\0" + _ttr3(t[1]) + b"\x01", 2))
    sha = _sha(path)
    with pytest.raises(FormatError, match="note list"):
        _txn(path, ULIB, lambda p: p.compress())
    assert _sha(path) == sha


def test_compress_refuses_overlay_with_uncounted_ttrn(fixture_images):
    """OVLY/SCTR load module whose PDS2TTRN is non-zero but whose C byte counts only one TTRN:
    compress could not relocate it, so it is refused before anything is staged."""
    path = fixture_images["cckd_zlib"]
    _txn(path, ULIB, lambda p: _add_u(
        p, "OVLYMOD", [b"\x01" * 100] * 2,
        lambda t: _ttr3(t[0]) + b"\0" + _ttr3(t[1]) + b"\x00" + b"\x20\x00", 1))
    sha = _sha(path)
    with pytest.raises(UnsupportedError, match="OVLYMOD"):
        _txn(path, ULIB, lambda p: p.compress())
    assert _sha(path) == sha
