"""Task 15: Session (open, recover, backup, read-only, operations)."""

import hashlib
import os

import pytest

from ckdoffline import ebcdic
from ckdoffline.dataset.pds import Pds
from ckdoffline.errors import (CkdError, NotFoundError, ReadOnlyError, UnsupportedError,
                               ValidationError)
from ckdoffline.image.rawfile import RawFile
from ckdoffline.session import Session

from oracle import dasdcat

FB = "TEST.FB80.PDS"
VB = "TEST.VB.PDS"
ULIB = "TEST.LOAD"
FBSEQ = "TEST.FB.SEQ"
VBSEQ = "TEST.VB.SEQ"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _baks(path):
    return sorted(path.parent.glob(path.name + ".bak-*"))


def _enc(s, n=80):
    return ebcdic.from_text(s.ljust(n), "1047")


@pytest.fixture
def img(fixture_images):
    return fixture_images["cckd_zlib"]


# -- backup / modes ----------------------------------------------------------------------------
def test_backup_made_once(img):
    sha0 = _sha(img)
    s = Session.open(img)
    assert s.info["backup_path"] is None and _baks(img) == []
    s.save_text(FB, "A", ["one"])
    s.save_text(FB, "B", ["two"])
    [bak] = _baks(img)
    assert _sha(bak) == sha0
    assert s.info["backup_path"] == str(bak)
    s.close()
    assert _sha(img) != sha0


def test_no_backup_flag(img):
    s = Session.open(img, backup=False)
    s.save_text(FB, "A", ["one"])
    s.close()
    assert _baks(img) == []


def test_readonly_refuses_writes(img):
    sha0 = _sha(img)
    s = Session.open(img, mode="ro")
    assert s.info["mode"] == "ro"
    for call in (lambda: s.save_text(FB, "A", ["x"]), lambda: s.new_member(FB, "A"),
                 lambda: s.delete_member(FB, "A"), lambda: s.rename_member(FB, "A", "B"),
                 lambda: s.patch_block(ULIB, "A", 0, "00"), lambda: s.compress(FB)):
        with pytest.raises(ReadOnlyError):
            call()
    assert s.members(FB) == []
    s.close()
    assert _sha(img) == sha0 and _baks(img) == []


def test_opened_flag_requires_confirm(img):
    with open(img, "r+b") as f:
        f.seek(512 + 3)                         # CDEVHDR options byte (see cckd.py OPT_OPENED)
        o = f.read(1)[0]
        f.seek(512 + 3)
        f.write(bytes([o | 0x80]))
    s = Session.open(img)
    assert s.info["warning"] == ("CCKD 'opened' flag set: image may be in use by an emulator, "
                                 "or the emulator crashed")
    sha0 = _sha(img)
    with pytest.raises(CkdError):
        s.save_text(FB, "A", ["x"])
    assert _sha(img) == sha0 and _baks(img) == []
    s.confirm_rw()
    s.save_text(FB, "A", ["x"])
    assert [m["name"] for m in s.members(FB)] == ["A"]
    s.close()


def test_no_warning_normally(img):
    s = Session.open(img)
    assert s.info["warning"] is None and s.info["recovered"] is False
    assert s.info["volser"] == "TEST01" and s.info["mode"] == "rw"
    assert s.info["fmt"] and s.info["cyls"] == 100 and s.info["free_tracks"] > 0
    s.close()


# -- recovery / rollback -----------------------------------------------------------------------
def test_recover_on_open(img, monkeypatch):
    sha0 = _sha(img)
    raw = RawFile(img, True)
    raw.begin()
    for i in range(6):
        raw.write(i * 500 + 2000, bytes([0xEE]) * 40)

    def crashing_apply(self, ranges):
        for off, data in ranges[:len(ranges) // 2]:
            os.pwrite(self._fd, data, off)
        raise RuntimeError("simulated crash")

    def no_restore(fd, size, items):
        raise RuntimeError("restore failed")
    monkeypatch.setattr(RawFile, "_apply_ranges", crashing_apply)
    monkeypatch.setattr(RawFile, "_restore", staticmethod(no_restore))
    with pytest.raises(RuntimeError):
        raw.commit()
    monkeypatch.undo()
    raw.close()
    assert _sha(img) != sha0 and (img.parent / (img.name + ".journal")).exists()
    s = Session.open(img)
    assert s.info["recovered"] is True
    s.close()
    assert _sha(img) == sha0
    assert not (img.parent / (img.name + ".journal")).exists()


def test_failed_save_rolls_back(img, monkeypatch):
    import ckdoffline.session as sess
    s = Session.open(img, backup=False)
    sha0 = _sha(img)
    real = sess.Pds.save_member

    def boom(self, name, records, userid):
        real(self, name, records, userid)       # stage everything, then fail
        raise RuntimeError("injected")
    monkeypatch.setattr(sess.Pds, "save_member", boom)
    with pytest.raises(RuntimeError, match="injected"):
        s.save_text(FB, "A", ["x"])
    monkeypatch.undo()
    assert _sha(img) == sha0
    assert not (img.parent / (img.name + ".journal")).exists()
    s.save_text(FB, "A", ["x"])                 # session still usable
    s.close()


def test_backup_failure_refuses_write(img, monkeypatch):
    import ckdoffline.session as sess
    monkeypatch.setattr(sess.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(sess.shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    s = Session.open(img)
    sha0 = _sha(img)
    with pytest.raises(CkdError, match="backup"):
        s.save_text(FB, "A", ["x"])
    assert _sha(img) == sha0
    s.close()


# -- reading -----------------------------------------------------------------------------------
def test_datasets_and_members(img):
    s = Session.open(img, mode="ro")
    names = {d.dsname: d for d in s.datasets()}
    assert FB in names and FBSEQ in names
    assert s.members(FB) == []
    s.close()


def test_member_listing_fields(img):
    s = Session.open(img, userid="abc")
    s.save_text(FB, "mem1", ["hello"])
    [m] = s.members(FB)
    assert m["name"] == "MEM1" and m["alias"] is False
    assert len(m["ttr"]) == 6 and int(m["ttr"], 16) > 0 and m["ttr"] == m["ttr"].upper()
    assert m["version"] == "01.00" and m["lines"] == 1 and m["userid"] == "ABC"
    assert m["created"] == m["changed"][:10]
    import json
    json.dumps(m)
    s.close()


def test_default_userid(img, monkeypatch):
    monkeypatch.setenv("USER", "longusername")
    s = Session.open(img)
    assert s.userid == "LONGUSER"
    monkeypatch.delenv("USER")
    assert Session.open(img, mode="ro").userid == "CKDOFF"
    s.close()


def test_text_roundtrip_fb_untouched_bytes(img):
    s = Session.open(img)
    odd = bytes([0x00, 0x05, 0xFF, 0x41, 0xC1]) + b"\x40" * 75
    s.save_text(FB, "ODD", [{"raw": odd.hex()}, "plain text"])
    r = s.read_text(FB, "ODD")
    assert r["recfm"] == "FB" and r["lrecl"] == 80 and len(r["lines"]) == 2
    assert r["raw_lines"][0] == odd.hex()
    assert r["lines"][1] == "plain text".ljust(80)            # FB: padded, not stripped
    # send everything back untouched
    s.save_text(FB, "ODD", [{"raw": x} for x in r["raw_lines"]])
    assert s.read_text(FB, "ODD")["raw_lines"] == r["raw_lines"]
    s.close()


def test_fb_overlength_rejected(img):
    s = Session.open(img, backup=False)
    sha0 = _sha(img)
    with pytest.raises(ValidationError, match="line 2") as e:
        s.save_text(FB, "BIG", ["ok", "x" * 81])
    assert e.value.line_no == 2
    assert _sha(img) == sha0
    s.close()


def test_vb_lines_not_padded(img):
    s = Session.open(img)
    s.save_text(VB, "V1", ["abc", "", "defgh"])
    r = s.read_text(VB, "V1")
    # an emptied V line is saved as one blank, as ISPF does (final review M3)
    assert r["recfm"] == "VB" and r["lines"] == ["abc", " ", "defgh"]
    assert r["raw_lines"] == [_enc("abc", 3).hex(), _enc("", 1).hex(), _enc("defgh", 5).hex()]
    with pytest.raises(ValidationError):
        s.save_text(VB, "V2", ["x" * 252])        # LRECL 255 - 4 = 251
    s.close()


def test_codepage_037(img):
    s = Session.open(img, codepage="037")
    s.save_text(VB, "CP", ["a[b]"])
    assert s.read_text(VB, "CP")["raw_lines"] == [ebcdic.from_text("a[b]", "037").hex()]
    s.close()


def test_sequential_text(img):
    s = Session.open(img)
    s.save_text(FBSEQ, None, ["first", "second"])
    r = s.read_text(FBSEQ)
    assert [l.rstrip() for l in r["lines"]] == ["first", "second"]
    s.save_text(VBSEQ, None, ["a", "bb"])
    assert s.read_text(VBSEQ)["lines"] == ["a", "bb"]
    with pytest.raises(ValidationError):
        s.read_text(FB)                          # PDS needs a member
    with pytest.raises(ValidationError):
        s.read_text(FBSEQ, "X")                  # PS has no members
    s.close()


def test_unknown_dataset_and_member(img):
    s = Session.open(img)
    with pytest.raises(NotFoundError):
        s.read_text("NO.SUCH.DS")
    with pytest.raises(NotFoundError):
        s.read_text(FB, "NOPE")
    s.close()


def test_new_rename_delete_member(img):
    s = Session.open(img)
    s.new_member(FB, "empty")
    assert s.read_text(FB, "EMPTY")["lines"] == []
    with pytest.raises(ValidationError):
        s.new_member(FB, "EMPTY")                # exists
    with pytest.raises(ValidationError):
        s.new_member(FB, "1BAD")
    s.rename_member(FB, "empty", "other")
    assert [m["name"] for m in s.members(FB)] == ["OTHER"]
    s.delete_member(FB, "OTHER")
    assert s.members(FB) == []
    s.close()


def test_compress(img):
    s = Session.open(img)
    for n in range(5):
        s.save_text(FB, "HOT", [f"v{n} {i}" for i in range(50)])
    before = s.read_text(FB, "HOT")
    res = s.compress(FB)
    assert {"tracks_before", "tracks_after", "members"} <= res.keys()
    assert s.read_text(FB, "HOT") == before
    s.close()


def test_recfm_u(img):
    s = Session.open(img)
    with pytest.raises(UnsupportedError):
        s.new_member(ULIB, "X")
    with pytest.raises(UnsupportedError):
        s.read_text(ULIB, "X")
    s.close()


def test_read_blocks_and_patch_block(img):
    s = Session.open(img)
    s.save_text(FB, "M", ["abc", "def"])
    [blk] = s.read_blocks(FB, "M")
    assert blk == (_enc("abc") + _enc("def")).hex()
    new = bytes.fromhex(blk)[:-1] + b"\x01"
    s.patch_block(FB, "M", 0, new.hex())
    assert s.read_blocks(FB, "M") == [new.hex()]
    with pytest.raises(ValidationError):
        s.patch_block(FB, "M", 0, "00")           # length change
    assert [x.hex() for x in [bytes.fromhex(b) for b in s.read_blocks(FBSEQ)]] == s.read_blocks(FBSEQ)
    s.close()


# -- backup naming and failure cleanup ----------------------------------------------------------
def test_backup_never_overwrites_existing(img, monkeypatch):
    import ckdoffline.session as sess

    class FixedDT:
        @staticmethod
        def now():
            import datetime as d
            return d.datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(sess, "datetime", FixedDT)
    first = img.with_name(img.name + ".bak-20260102-030405")
    first.write_bytes(b"precious")
    second = img.with_name(img.name + ".bak-20260102-030405-1")
    second.write_bytes(b"also precious")
    s = Session.open(img)
    s.save_text(FB, "A", ["x"])
    assert first.read_bytes() == b"precious" and second.read_bytes() == b"also precious"
    assert s.backup_path == img.with_name(img.name + ".bak-20260102-030405-2")
    assert _sha(s.backup_path) != _sha(img)
    s.close()


def test_partial_backup_removed_on_failure(img, monkeypatch):
    import ckdoffline.session as sess
    monkeypatch.setattr(sess.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))

    def partial(src, dst, *a, **k):
        open(dst, "wb").write(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(sess.shutil, "copyfile", partial)
    s = Session.open(img)
    with pytest.raises(CkdError, match="backup"):
        s.save_text(FB, "A", ["x"])
    assert _baks(img) == []
    s.close()


def test_backup_failure_keeps_preexisting_file(img, monkeypatch):
    import ckdoffline.session as sess
    monkeypatch.setattr(sess.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(sess.shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    other = img.with_name(img.name + ".bak-19990101-000000")
    other.write_bytes(b"keep")
    s = Session.open(img)
    with pytest.raises(CkdError, match="backup"):
        s.save_text(FB, "A", ["x"])
    assert other.read_bytes() == b"keep" and _baks(img) == [other]
    s.close()


def test_info_reports_codepage(img):
    """The UI decodes hex views with the session's code page, so info must say which."""
    with Session.open(img, codepage="037") as s:
        assert s.info["codepage"] == "037"


# -- follow-up: atomic multi-block patch, DSCB decode ------------------------------------------
def _three_blocks(s):
    s.save_text(FB, "M", [f"LINE {i:05d}" for i in range(800)])   # 349 records per block
    blocks = [bytes.fromhex(b) for b in s.read_blocks(FB, "M")]
    assert len(blocks) == 3
    return blocks


def test_patch_blocks_one_transaction(img):
    s = Session.open(img)
    b = _three_blocks(s)
    new0, new2 = b"\xc1" + b[0][1:], b[2][:-1] + b"\xc2"
    s.patch_blocks(FB, "M", [{"index": 0, "hex": new0.hex()}, {"index": 2, "hex": new2.hex()}])
    assert [bytes.fromhex(x) for x in s.read_blocks(FB, "M")] == [new0, b[1], new2]
    [m] = s.members(FB)
    assert m["version"] == "01.01"                       # one save: mod + 1, not + 2
    s.close()


def test_patch_blocks_same_track(img):
    """Two blocks on the SAME track patched in one call: the track is rebuilt once with both."""
    s = Session.open(img)
    b = _three_blocks(s)
    ttrs = [ttr for ttr, _ in Pds(s.volume, FB).read_member_blocks("M")]
    i, j = next((i, i + 1) for i in range(2) if ttrs[i][0] == ttrs[i + 1][0])
    new = {i: b"\xd1" + b[i][1:-1] + b"\xd2", j: b"\xd3" + b[j][1:-1] + b"\xd4"}
    s.patch_blocks(FB, "M", [{"index": k, "hex": v.hex()} for k, v in new.items()])
    want = [new.get(k, b[k]) for k in range(3)]
    assert [bytes.fromhex(x) for x in s.read_blocks(FB, "M")] == want
    s.close()
    with Session.open(img, "ro") as r:                   # from disk, not a cache
        assert [bytes.fromhex(x) for x in r.read_blocks(FB, "M")] == want
        assert [ttr for ttr, _ in Pds(r.volume, FB).read_member_blocks("M")] == ttrs
    assert dasdcat(img, FB, "M", ascii=False) == b"".join(want)


@pytest.mark.parametrize("bad", [
    {"index": 1, "hex": "00"},                           # length change
    {"index": 7, "hex": "00"},                           # no such block
    {"index": 1, "hex": "zz"},                           # not hex
    {"index": 0, "hex": None},                           # duplicate index (with the good one)
])
def test_patch_blocks_all_or_nothing(img, bad):
    s = Session.open(img)
    b = _three_blocks(s)
    sha0 = _sha(img)
    good = {"index": 0, "hex": (b"\xc1" + b[0][1:]).hex()}
    if bad["hex"] is None:
        bad = dict(good)
    with pytest.raises(ValidationError):
        s.patch_blocks(FB, "M", [good, bad])
    assert _sha(img) == sha0
    assert [bytes.fromhex(x) for x in s.read_blocks(FB, "M")] == b
    with pytest.raises(ValidationError):
        s.patch_blocks(FB, "M", [])
    s.close()


def test_dscb_decode(img):
    s = Session.open(img)
    d = s.dscb(FB)
    assert d["dsname"] == FB and d["volser"] == "TEST01"
    assert d["dsorg_str"] == "PO" and d["dsorg"] == "0200"
    assert d["recfm_str"] == "FB" and d["lrecl"] == 80 and d["blksize"] == 27920
    assert d["keylen"] == 0 and d["rkp"] == 0
    assert d["credt"] and len(d["credt"]) == 10            # ISO date
    assert set(d) >= {"expdt", "refdt", "optcd", "dsind", "dsind_flags", "scalo", "lstar",
                      "trbal", "noepv", "smsfg", "smsfg_flags", "extents", "f1_cchhr"}
    assert d["noepv"] == len(d["extents"]) == 1
    [x] = d["extents"]
    assert (x["source"], x["seq"], x["tracks"]) == ("F1", 0, 5)
    assert {"bcyl", "bhead", "ecyl", "ehead", "type"} <= set(x)
    s.save_text(FB, "A", ["x"])
    d = s.dscb(FB)
    assert "changed since backup" in d["dsind_flags"]
    assert d["refdt"] is not None
    with pytest.raises(NotFoundError):
        s.dscb("NO.SUCH.DS")
    s.close()


# -- final review: external changes to the image (Critical 1) ---------------------------------
@pytest.mark.parametrize("key", ["cckd_zlib", "ckd"])
def test_external_change_refuses_save(fixture_images, key):
    path = fixture_images[key]
    s1 = Session.open(path, backup=False)
    s1.datasets()
    assert s1.members(FB) == []
    with Session.open(path, backup=False) as s2:        # an independent writer
        s2.save_text(FB, "NEWMEM", ["new data"])
    sha = _sha(path)
    with pytest.raises(CkdError, match="changed on disk"):
        s1.save_text(FB, "A", ["stale"])
    for call in (lambda: s1.new_member(FB, "B"), lambda: s1.delete_member(FB, "NEWMEM"),
                 lambda: s1.compress(FB)):
        with pytest.raises(CkdError, match="changed on disk"):
            call()
    assert _sha(path) == sha
    # reads drop the stale caches: the other writer's member is visible
    assert [m["name"] for m in s1.members(FB)] == ["NEWMEM"]
    s1.close()
    with Session.open(path, backup=False) as s3:
        assert s3.read_text(FB, "NEWMEM")["lines"][0].rstrip() == "new data"


def test_own_commits_do_not_count_as_external(img):
    s = Session.open(img, backup=False)
    s.save_text(FB, "A", ["one"])
    s.save_text(FB, "A", ["two"])
    s.delete_member(FB, "A")
    s.close()


def test_opened_flag_set_after_open_requires_confirm(img):
    s = Session.open(img, backup=False)
    assert s.info["warning"] is None
    st = os.stat(img)
    with open(img, "r+b") as f:                 # Hercules opens it (flag only, same mtime)
        f.seek(512 + 3)
        o = f.read(1)[0]
        f.seek(512 + 3)
        f.write(bytes([o | 0x80]))
    os.utime(img, ns=(st.st_atime_ns, st.st_mtime_ns))
    sha = _sha(img)
    with pytest.raises(CkdError, match="opened"):
        s.save_text(FB, "A", ["x"])
    assert _sha(img) == sha
    assert s.info["warning"] is not None
    s.confirm_rw()
    s.save_text(FB, "A", ["x"])
    s.close()


def _patch_f1(path, dsn, **fields):
    """Set Format-1 fields of a dataset in place (outside any session)."""
    from ckdoffline.image.base import open_image
    from ckdoffline.vtoc.vtoc import Volume
    img = open_image(path, True)
    try:
        vol = Volume(img)
        c, f1 = vol.f1(dsn)
        for k, v in fields.items():
            setattr(f1, k, v)
        img.begin()
        vol.write_dscb(c, f1)
        img.commit()
    finally:
        img.close()


# -- final review: free space errors do not block a volume (Important 2) -------------------------
def test_info_survives_bad_free_space_map(f5_image):
    from ckdoffline.image.base import open_image
    from ckdoffline.vtoc.vtoc import Volume
    img = open_image(f5_image, True)
    vol = Volume(img)
    e = vol.vtoc_extent
    f5c = (e.bcyl, e.bhead, 2)
    f5 = vol.read_dscb(f5c)
    f5.extents = f5.extents[1:]                 # F5 out of sync with the extents
    f4 = vol.read_dscb(vol.vtoc_cchhr)
    f4.vtoci |= 0x04                            # DS4DIRF: z/OS has to repair the VTOC
    img.begin()
    vol.write_dscb(f5c, f5)
    vol.write_dscb(vol.vtoc_cchhr, f4)
    img.commit()
    img.close()
    s = Session.open(f5_image, backup=False)
    info = s.info
    assert info["free_tracks"] is None
    assert "out of sync" in info["free_space_error"]
    s.save_text(FB, "A", ["one"])               # in-place work still goes through
    s.save_text(FB, "A", ["two"])               # same-size member edit
    assert s.read_text(FB, "A")["lines"][0].rstrip() == "two"
    assert s.info["free_space_error"]
    s.close()


def test_info_free_space_normally(f5_image):
    with Session.open(f5_image, backup=False) as s:
        assert s.info["free_tracks"] > 0 and s.info["free_space_error"] is None
        assert s.info["device"] == "3390"


# -- final review: hex patch of a sequential RECFM U dataset (Important 4) ----------------------
def test_patch_sequential_recfm_u(img):
    _patch_f1(img, FBSEQ, recfm=0xC0)           # make TEST.FB.SEQ a PS RECFM U dataset
    s = Session.open(img, backup=False)
    blocks = [bytes.fromhex(b) for b in s.read_blocks(FBSEQ)]
    assert len(blocks) > 2
    new0 = b"\xC1" * len(blocks[0])
    new2 = bytes(len(blocks[2]))
    s.patch_blocks(FBSEQ, None, [{"index": 0, "hex": new0.hex()}, {"index": 2, "hex": new2.hex()}])
    after = [bytes.fromhex(b) for b in s.read_blocks(FBSEQ)]
    assert after == [new0, blocks[1], new2] + blocks[3:]
    assert "changed since backup" in s.dscb(FBSEQ)["dsind_flags"]
    sha = _sha(img)
    with pytest.raises(ValidationError, match="block 2 is"):           # 1-based (M5)
        s.patch_blocks(FBSEQ, None, [{"index": 0, "hex": new0.hex()}, {"index": 1, "hex": "00"}])
    with pytest.raises(ValidationError, match=f"there is no block {len(blocks) + 1}"):
        s.patch_block(FBSEQ, None, len(blocks), "00")
    with pytest.raises(ValidationError, match="block 1: data is not valid hex"):
        s.patch_block(FBSEQ, None, 0, "zz")
    with pytest.raises(ValidationError):
        s.patch_block(FBSEQ, "MEM", 0, new0.hex())                     # PS has no members
    with pytest.raises(UnsupportedError):
        s.patch_block(VBSEQ, None, 0, "00")                             # not RECFM U: text
    assert _sha(img) == sha
    s.close()
    from oracle import cckdcdsk
    assert cckdcdsk(img)[0] == 0
    with Session.open(img, mode="ro") as s2:
        assert [bytes.fromhex(b) for b in s2.read_blocks(FBSEQ)] == after


def test_member_patch_messages_are_one_based(img):
    s = Session.open(img, backup=False)
    s.save_text(FB, "M", ["abc"])
    with pytest.raises(ValidationError, match="there is no block 2"):
        s.patch_block(FB, "M", 1, "00")
    with pytest.raises(ValidationError, match="block 1 is 80 bytes"):
        s.patch_block(FB, "M", 0, "00")
    with pytest.raises(ValidationError, match="block 1 is patched twice"):
        s.patch_blocks(FB, "M", [{"index": 0, "hex": "00"}, {"index": 0, "hex": "00"}])
    s.close()


# -- final review minors ------------------------------------------------------------------------
def test_large_format_over_65535_tracks_not_editable(img):
    _patch_f1(img, FBSEQ, flag1=0x08, ttthi=1)                  # DS1LARGE, tracks used > 65535
    with Session.open(img, backup=False) as s:
        [d] = [d for d in s.datasets() if d.dsname == FBSEQ]
        assert not d.editable and "65535" in d.reason
        with pytest.raises(UnsupportedError):
            s.save_text(FBSEQ, None, ["x"])


def test_keyed_pds_member_save_refused(img):
    _patch_f1(img, FB, keylen=8)
    with Session.open(img, backup=False) as s:
        with pytest.raises(UnsupportedError, match="keyed"):
            s.save_text(FB, "A", ["x"])
