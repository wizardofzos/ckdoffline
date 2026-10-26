"""Task 13: writing PDS members (add, replace, delete, rename, hex patch) and directory upkeep.

Directory layout written (matches z/OS and dasdload): entries sorted by EBCDIC name and
packed from the first block; each used block's key is its highest name; the X'FF'*8 entry (TTR 0,
C 0) ends the directory and its block's key is X'FF'*8; blocks after it have key X'00'*8 and 256
zero data bytes. Only blocks whose bytes change are rewritten.
"""

import hashlib
from datetime import date, datetime

import pytest

from ckdoffline import ebcdic
from ckdoffline.dataset.pds import (END_NAME, IspfStats, Pds, build_ispf, parse_ispf,
                                    validate_member_name)
from ckdoffline.errors import DirectoryFullError, NoSpaceError, ValidationError
from ckdoffline.image.base import open_image
from ckdoffline.track import fits_on_track
from ckdoffline.vtoc.vtoc import Volume

from oracle import cckdcdsk, dasdcat, dasdcat_members, dasdls

FB = "TEST.FB80.PDS"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _txn(path, dsn, fn):
    """Run fn(pds) in one committed transaction (rolled back on error)."""
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


def _set_dir(path, dsn, fn):
    """Replace the raw directory entries with fn(raws), to set up odd states."""
    def run(p):
        d = p._read_dir()
        p._stage_directory(d, p._plan_directory(d, fn(d.raws)))
    _txn(path, dsn, run)


def _dir_raw(pds):
    """[(key, data)] of every directory block."""
    out = []
    for _, rec in pds.io.blocks():
        if len(rec.key) != 8:
            break
        out.append((rec.key, rec.data))
    return out


def _assert_directory_ok(pds, trailing_zero=True):
    """Entries in EBCDIC order across blocks, key = highest name per block, FF terminator, and
    (when trailing_zero) all-zero blocks after the terminator block."""
    names = []
    done = False
    for key, data in _dir_raw(pds):
        if done:
            if trailing_zero:
                assert key == bytes(8) and data == bytes(256)
            continue
        used = int.from_bytes(data[:2], "big")
        assert 14 <= used <= 256
        pos, last = 2, None
        while pos < used:
            last = data[pos:pos + 8]
            names.append(last)
            if last == END_NAME:
                assert data[pos + 8:pos + 12] == bytes(4)
                assert pos + 12 == used
                done = True
                break
            pos += 12 + 2 * (data[pos + 11] & 0x1F)
        assert pos <= used
        assert key == last
    assert done and names[-1] == END_NAME
    assert names == sorted(names) and len(set(names)) == len(names)


# -- names ------------------------------------------------------------------------------------
def test_member_name_validation():
    assert validate_member_name("abc") == "ABC"
    assert validate_member_name("@#$X") == "@#$X"
    assert validate_member_name("A1234567") == "A1234567"
    assert Pds.validate_member_name("x") == "X"
    for bad in ("1ABC", "TOOLONGNM", "A-B", "", "A B", "straße", "ÉA", "A.B"):
        with pytest.raises(ValidationError):
            validate_member_name(bad)


# -- add --------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["ckd", "cckd_zlib", "cckd_bz2", "cckd64"])
def test_add_member_fixture(fixture_images, key):
    path = fixture_images[key]
    lines = [f"//STEP{i} EXEC PGM=IEFBR14" for i in range(40)]
    _txn(path, FB, lambda p: p.save_member("newmem", _text(lines), "IBMUSER"))
    pds = _pds(path, FB)
    assert [e.name for e in pds.directory()] == ["NEWMEM"]
    assert pds.read_member("NEWMEM") == _text(lines)
    assert dasdcat(path, FB, "NEWMEM", ascii=False) == b"".join(_text(lines))
    assert [m[0] for m in dasdcat_members(path, FB)] == ["NEWMEM"]
    s = parse_ispf(pds.directory()[0].userdata)
    assert (s.version, s.mod, s.lines, s.init_lines, s.mod_lines, s.userid) == (1, 0, 40, 40, 0, "IBMUSER")
    assert s.created == date.today()
    # the data follows the EOF that ends the directory (R4); a new EOF follows it
    assert pds.directory()[0].ttr == (0, 5)
    assert pds.io.end_of_data() == (0, 6)
    assert [r.rec for r in pds.io.read_rel_track(0) if not r.data] == [4, 6]
    assert pds.io.f1.dsind & 0x02
    _assert_directory_ok(pds)
    assert "TEST.FB80.PDS" in dasdls(path)
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_add_member_vb(fixture_images):
    path = fixture_images["cckd_zlib"]
    recs = [b"\xc1" * n for n in (1, 80, 251, 0, 7)]
    _txn(path, "TEST.VB.PDS", lambda p: p.save_member("V1", recs, "U"))
    assert _pds(path, "TEST.VB.PDS").read_member("V1") == recs
    assert dasdcat(path, "TEST.VB.PDS", "V1", ascii=False)


def test_add_zero_records(fixture_images):
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("EMPTY", [], "U"))
    pds = _pds(path, FB)
    assert pds.read_member("EMPTY") == []
    e = pds.directory()[0]
    assert e.ttr == (0, 5) and pds.io.end_of_data() == (0, 5)   # TTR = the member's EOF
    assert parse_ispf(e.userdata).lines == 0
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_overlength_line_refused_nothing_staged(fixture_images):
    path = fixture_images["cckd_zlib"]
    before = _sha(path)
    with pytest.raises(ValidationError):
        _txn(path, FB, lambda p: p.save_member("BAD", [b"x" * 81], "U"))
    assert _sha(path) == before


# -- replace ----------------------------------------------------------------------------------


def test_replace_without_stats_gets_fresh_stats(fixture_images):
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("M", _text(["A"]), "U"))
    # strip the stats, as a member written by IEBGENER would have none
    _set_dir(path, FB, lambda raws: [raws[0][:11] + b"\x00"])
    assert _pds(path, FB).directory()[0].userdata == b""
    _txn(path, FB, lambda p: p.save_member("M", _text(["A", "B"]), "ME"))
    s = parse_ispf(_pds(path, FB).directory()[0].userdata)
    assert (s.version, s.mod, s.lines, s.init_lines, s.userid) == (1, 0, 2, 2, "ME")


def test_replace_keeps_aliases_on_old_data(fixture_images):
    """ISPF/BPAM behaviour: replacing a main member gives it a new TTR; its aliases keep the old TTR
    (and so the old data)."""
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("MAIN", _text(["OLD"]), "U"))
    _set_dir(path, FB, lambda raws: raws + [
        ebcdic.from_text("ALIAS".ljust(8), "1047") + raws[0][8:11] + b"\x80"])
    _txn(path, FB, lambda p: p.save_member("MAIN", _text(["NEW"]), "U"))
    pds = _pds(path, FB)
    assert pds.read_member("MAIN") == _text(["NEW"])
    assert pds.read_member("ALIAS") == _text(["OLD"])
    alias = pds._find("ALIAS")
    assert alias.alias and alias.userdata == b""


def test_pds_grows_into_secondary(f5_image):
    path = f5_image
    vol = Volume(open_image(path, False))
    tracks0 = Pds(vol, FB).io.total_tracks
    recs = _text([f"RECORD {i}" for i in range(4000)])     # 12 blocks of 27920: > 5 tracks
    _txn(path, FB, lambda p: p.save_member("BIG", recs, "U"))
    pds = _pds(path, FB)
    assert pds.io.total_tracks > tracks0
    assert pds.read_member("BIG") == recs
    assert dasdcat(path, FB, "BIG", ascii=False) == b"".join(recs)
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


# -- delete / rename --------------------------------------------------------------------------
def test_delete_and_rename(fixture_images):
    path = fixture_images["cckd_zlib"]
    def setup(p):
        for n in ("AAA", "BBB", "CCC"):
            p.save_member(n, _text([n]), "U")
    _txn(path, FB, setup)
    _txn(path, FB, lambda p: p.delete_member("bbb"))
    assert [e.name for e in _pds(path, FB).directory()] == ["AAA", "CCC"]
    _txn(path, FB, lambda p: p.rename_member("AAA", "zzz"))
    pds = _pds(path, FB)
    assert [e.name for e in pds.directory()] == ["CCC", "ZZZ"]
    assert pds.read_member("ZZZ") == _text(["AAA"])
    _assert_directory_ok(pds)
    before = _sha(path)
    with pytest.raises(ValidationError):
        _txn(path, FB, lambda p: p.rename_member("CCC", "ZZZ"))
    with pytest.raises(ValidationError):
        _txn(path, FB, lambda p: p.rename_member("CCC", "9X"))
    with pytest.raises(ValidationError):
        _txn(path, FB, lambda p: p.save_member("BAD-NAME", [], "U"))
    assert _sha(path) == before
    assert [m[0] for m in dasdcat_members(path, FB)] == ["CCC", "ZZZ"]
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_directory_full(fixture_images):
    path = fixture_images["cckd_zlib"]
    # 42-byte entries (ISPF stats): 6 + 6 + 5 entries and the terminator fill 3 blocks
    def fill(p):
        for i in range(17):
            p.save_member(f"M{i:02d}", _text([str(i)]), "U")
    _txn(path, FB, fill)
    pds = _pds(path, FB)
    assert pds.dir_blocks_used() == pds.dir_block_count() == 3
    _assert_directory_ok(pds)
    before = _sha(path)
    with pytest.raises(DirectoryFullError):
        _txn(path, FB, lambda p: p.save_member("ONEMORE", _text(["X"]), "U"))
    assert _sha(path) == before
    # a replace still fits (same entry size)
    _txn(path, FB, lambda p: p.save_member("M05", _text(["Y"]), "U"))
    assert _pds(path, FB).read_member("M05") == _text(["Y"])
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_rename_across_directory_blocks(fixture_images):
    path = fixture_images["cckd_zlib"]
    names = [f"A{i:02d}" for i in range(13)]       # 6 + 6 + 1: three blocks
    _txn(path, FB, lambda p: [p.save_member(n, _text([n]), "U") for n in names])
    pds = _pds(path, FB)
    assert pds.dir_blocks_used() == 3
    keys0 = [k for k, _ in _dir_raw(pds)]
    # A00 (block 1) moves to the end (block 3); everything shifts one place
    _txn(path, FB, lambda p: p.rename_member("A00", "Z00"))
    pds = _pds(path, FB)
    want = names[1:] + ["Z00"]
    assert [e.name for e in pds.directory()] == want
    assert [m[0] for m in dasdcat_members(path, FB)] == want
    assert pds.read_member("Z00") == _text(["A00"])
    _assert_directory_ok(pds)
    keys = [k for k, _ in _dir_raw(pds)]
    assert keys != keys0 and keys[2] == END_NAME
    assert keys[0] == ebcdic.from_text("A06".ljust(8), "1047")
    # an add that lands in the last block (EBCDIC: digits sort after letters, so '#' < 'A' < 'Z' < '9')
    _txn(path, FB, lambda p: p.save_member("ZZ9", _text(["Q"]), "U"))   # 'Z' < '0': ZZ9 < Z00
    _txn(path, FB, lambda p: p.save_member("Z99", _text(["Q"]), "U"))
    pds = _pds(path, FB)
    assert [e.name for e in pds.directory()][-3:] == ["ZZ9", "Z00", "Z99"]
    _assert_directory_ok(pds)
    _txn(path, FB, lambda p: p.save_member("#A", _text(["Q"]), "U"))
    pds = _pds(path, FB)
    assert pds.directory()[0].name == "#A"
    _assert_directory_ok(pds)
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_unchanged_blocks_not_rewritten(fixture_images):
    path = fixture_images["cckd_zlib"]
    names = [f"A{i:02d}" for i in range(13)]
    _txn(path, FB, lambda p: [p.save_member(n, _text([n]), "U") for n in names])
    first = _dir_raw(_pds(path, FB))[0]
    # A12 sits alone in block 3: renaming it to A99 changes block 3 only
    def plan(p):
        d = p._read_dir()
        return p._plan_directory(d, [r if r[:8] != ebcdic.from_text("A12".ljust(8), "1047")
                                     else ebcdic.from_text("A99".ljust(8), "1047") + r[8:]
                                     for r in d.raws])
    plan = _txn(path, FB, plan)
    assert [i for i, _ in plan] == [2]
    _txn(path, FB, lambda p: p.rename_member("A12", "A99"))
    assert _dir_raw(_pds(path, FB))[0] == first


# -- RECFM U / hex patch ----------------------------------------------------------------------


def test_hex_patch_text_member_updates_stats(fixture_images):
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("T", _text(["ABC"]), "U"))
    s0 = parse_ispf(_pds(path, FB).directory()[0].userdata)
    blk = _pds(path, FB).read_member_blocks("T")[0][1]
    _txn(path, FB, lambda p: p.patch_member_block("T", 0, b"\xc4" + blk[1:], "PATCHER"))
    pds = _pds(path, FB)
    assert pds.read_member("T") == _text(["DBC"])
    s1 = parse_ispf(pds.directory()[0].userdata)
    assert s1.mod == s0.mod + 1 and s1.lines == 1 and s1.userid == "PATCHER"


def test_read_member_with_zero_ttr(fixture_images):
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("Z", _text(["A"]), "U"))
    _set_dir(path, FB, lambda raws: [r[:8] + b"\0\0\0" + r[11:] for r in raws])
    pds = _pds(path, FB)
    assert pds._find("Z").ttr == (0, 0)
    assert pds.read_member_blocks("Z") == []
    assert pds.read_member("Z") == []


def test_stats_mod_capped_at_99(fixture_images):
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("M", _text(["A"]), "U"))
    stats = IspfStats(1, 99, date(2020, 1, 1), datetime(2020, 1, 1), 1, 1, 0, "X")
    _set_dir(path, FB, lambda raws: [raws[0][:11] + bytes([15]) + build_ispf(stats)])
    _txn(path, FB, lambda p: p.save_member("M", _text(["B"]), "U"))
    s = parse_ispf(_pds(path, FB).directory()[0].userdata)
    assert s.mod == 99 and s.created == date(2020, 1, 1)


# -- where the first record of a member lands --------------------------------------------------
def _no_room_for_eof(pds):
    tt, r = pds.io.end_of_data()
    shapes = [(len(x.key), len(x.data)) for x in pds.io.read_rel_track(tt)]
    assert len(shapes) == r
    return not fits_on_track(shapes + [(0, 0)])


def test_zero_record_save_when_eof_opens_a_new_track(fixture_images):
    """Track 0 = 3 directory blocks, EOF, 27920 + 22400 bytes of BIG, EOF: no room for another
    EOF there, so an empty member's EOF (and its TTR) is R1 of track 1."""
    path = fixture_images["cckd_zlib"]
    _txn(path, FB, lambda p: p.save_member("BIG", _text([str(i) for i in range(629)]), "U"))
    pds = _pds(path, FB)
    assert pds.io.end_of_data() == (0, 7) and _no_room_for_eof(pds)
    _txn(path, FB, lambda p: p.save_member("EMPTY", [], "U"))
    pds = _pds(path, FB)
    assert pds._find("EMPTY").ttr == pds.io.end_of_data() == (1, 1)
    assert [len(r.data) for r in pds.io.read_rel_track(1)] == [0]
    assert pds.read_member("EMPTY") == []
    assert pds.read_member("BIG") == _text([str(i) for i in range(629)])
    assert dasdcat(path, FB, "EMPTY", ascii=False) == b""
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


@pytest.mark.parametrize("nrec", [0, 1])
def test_save_after_eof_at_highest_record(fixture_images, nrec):
    """The most records a 3390 track takes here: 3 directory blocks, EOF, 71 one-record blocks,
    EOF at R76 with no room after it. The next member starts at R1 of track 1."""
    path = fixture_images["cckd_zlib"]
    def small(p):
        p.io.write_blocks((0, 5), [b"\xc1" * 80] * 71, ending="PO")
        d = p._read_dir()
        p._stage_directory(d, p._plan_directory(
            d, [ebcdic.from_text("SMALL".ljust(8), "1047") + b"\x00\x00\x05\x00"]))
    _txn(path, FB, small)
    pds = _pds(path, FB)
    assert pds.io.end_of_data() == (0, 76) and _no_room_for_eof(pds)
    assert pds.read_member("SMALL") == [b"\xc1" * 80] * 71
    recs = _text(["ONE"])[:nrec]
    _txn(path, FB, lambda p: p.save_member("NEXT", recs, "U"))
    pds = _pds(path, FB)
    assert pds._find("NEXT").ttr == (1, 1)
    assert pds.read_member("NEXT") == recs
    assert pds.read_member("SMALL") == [b"\xc1" * 80] * 71
    assert dasdcat(path, FB, "NEXT", ascii=False) == b"".join(recs)
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_first_block_opens_a_new_track(fixture_images):
    """A (one 27920 block) leaves track 0 with room for an EOF but not another full block, so
    B's first block is R1 of track 1."""
    path = fixture_images["cckd_zlib"]
    a = _text([f"A{i}" for i in range(349)])
    b = _text([f"B{i}" for i in range(698)])           # two half-track blocks
    _txn(path, FB, lambda p: p.save_member("A", a, "U"))
    assert _pds(path, FB).io.end_of_data() == (0, 6)
    _txn(path, FB, lambda p: p.save_member("B", b, "U"))
    pds = _pds(path, FB)
    assert pds._find("B").ttr == (1, 1)
    assert pds.read_member("A") == a and pds.read_member("B") == b
    assert dasdcat(path, FB, "B", ascii=False) == b"".join(b)
    # (dasdcat_members can't confirm the start here: on cylinder 0 dasdcat reports the last block)
    assert [len(r.data) for r in pds.io.read_rel_track(0)][3:] == [0, 27920, 0]
    assert [len(r.data) for r in pds.io.read_rel_track(1)] == [27920, 27920]
    assert [len(r.data) for r in pds.io.read_rel_track(2)] == [0] and pds.io.end_of_data() == (2, 1)
    rc, out = cckdcdsk(path, 3)
    assert rc == 0, out


def test_unencodable_ttr_refused_before_staging(fixture_images, monkeypatch):
    path = fixture_images["cckd_zlib"]
    before = _sha(path)
    from ckdoffline.dataset.extents import DatasetIO
    monkeypatch.setattr(DatasetIO, "first_ttr", lambda self, *a: (0x10000, 1))
    with pytest.raises(NoSpaceError):
        _txn(path, FB, lambda p: p.save_member("M", _text(["A"]), "U"))
    assert _sha(path) == before


def test_mod_lines_counts_changed_lines(fixture_images):
    path = fixture_images["cckd_zlib"]
    lines = [f"LINE {i}" for i in range(10)]
    _txn(path, FB, lambda p: p.save_member("M", _text(lines), "U"))
    new = lines[:3] + ["CHANGED"] + lines[4:7] + lines[8:] + ["ADDED"]   # 1 changed, 1 removed, 1 added
    _txn(path, FB, lambda p: p.save_member("M", _text(new), "U"))
    s = parse_ispf(_pds(path, FB).directory()[0].userdata)
    assert (s.mod, s.lines, s.init_lines, s.mod_lines) == (1, 10, 10, 3)
    _txn(path, FB, lambda p: p.save_member("M", _text(new), "U"))
    s = parse_ispf(_pds(path, FB).directory()[0].userdata)
    assert (s.mod, s.mod_lines) == (2, 3)
