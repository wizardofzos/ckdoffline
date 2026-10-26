import hashlib
import os

import pytest

from ckdoffline.errors import CkdError, ReadOnlyError
from ckdoffline.image.rawfile import RawFile

ORIG = bytes(range(256)) * 16  # 4096 bytes


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "img.bin"
    p.write_bytes(ORIG)
    return p


def _no_restore(fd, size, items):
    raise RuntimeError("restore failed")


def kill_restore(monkeypatch):
    """Make the in-process rollback fail too, so the journal stays on disk
    exactly as after a real process death during apply."""
    monkeypatch.setattr(RawFile, "_restore", staticmethod(_no_restore))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_commit_applies(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(10, b"AAAA")
    rf.write(100, b"BB")
    rf.commit()
    assert not rf.in_transaction
    rf.close()
    data = path.read_bytes()
    assert data[10:14] == b"AAAA" and data[100:102] == b"BB"
    assert data[:10] == ORIG[:10] and data[14:100] == ORIG[14:100]
    assert len(data) == len(ORIG)
    assert not os.path.exists(str(path) + ".journal")


def test_rollback_discards(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(0, b"ZZZZ")
    rf.write(len(ORIG), b"grow")
    rf.rollback()
    assert not rf.in_transaction
    assert rf.read(0, 4) == ORIG[:4]
    assert rf.size == len(ORIG)
    assert sha(path) == hashlib.sha256(ORIG).hexdigest()


def test_read_sees_pending(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(5, b"XYZ")
    rf.write(6, b"__")  # overlap, later write wins
    assert rf.read(3, 8) == ORIG[3:5] + b"X__" + ORIG[8:11]
    assert path.read_bytes() == ORIG  # not yet on disk


def test_overlap_and_merge(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(10, b"aaaa")
    rf.write(12, b"bbbb")
    rf.write(20, b"cc")
    rf.write(14, b"dddddd")  # bridges to 20
    assert rf._pending == [(10, b"aabbdddddd" + b"cc")]
    rf.commit()
    assert path.read_bytes()[10:22] == b"aabbddddddcc"


def test_growth_read_and_commit(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(len(ORIG) + 4, b"tail")
    assert rf.size == len(ORIG) + 8
    assert rf.read(len(ORIG), 8) == b"\0\0\0\0tail"
    rf.commit()
    assert path.read_bytes()[len(ORIG):] == b"\0\0\0\0tail"


def test_read_only(path):
    rf = RawFile(path, False)
    with pytest.raises(ReadOnlyError):
        rf.begin()
    with pytest.raises(ReadOnlyError):
        rf.write(0, b"x")
    assert rf.read(0, 4) == ORIG[:4]


def test_misuse(path):
    rf = RawFile(path, True)
    with pytest.raises(CkdError):
        rf.write(0, b"x")  # outside a transaction
    with pytest.raises(CkdError):
        rf.commit()
    rf.begin()
    with pytest.raises(CkdError):
        rf.begin()


def test_crash_after_journal_restores(path, monkeypatch):
    before = sha(path)
    rf = RawFile(path, True)
    rf.begin()
    for i in range(6):
        rf.write(i * 500, bytes([0xEE]) * 40)
    rf.write(len(ORIG) + 100, b"growth")

    def crashing_apply(self, ranges):
        half = len(ranges) // 2
        for off, data in ranges[:half]:
            os.pwrite(self._fd, data, off)
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(RawFile, "_apply_ranges", crashing_apply)
    kill_restore(monkeypatch)
    with pytest.raises(RuntimeError):
        rf.commit()
    monkeypatch.undo()
    rf.close()
    assert sha(path) != before  # genuinely damaged
    assert os.path.exists(str(path) + ".journal")
    assert RawFile.recover(path) is True
    assert sha(path) == before
    assert not os.path.exists(str(path) + ".journal")
    assert RawFile.recover(path) is False


def test_torn_journal_ignored(path, monkeypatch):
    before = sha(path)
    rf = RawFile(path, True)
    rf.begin()
    rf.write(0, b"QQQQ")

    def crash(self, ranges):
        raise RuntimeError("crash before apply")

    monkeypatch.setattr(RawFile, "_apply_ranges", crash)
    kill_restore(monkeypatch)
    with pytest.raises(RuntimeError):
        rf.commit()
    monkeypatch.undo()
    rf.close()
    j = str(path) + ".journal"
    raw = open(j, "rb").read()
    open(j, "wb").write(raw[:-6])  # cut into the trailer
    assert RawFile.recover(path) is False
    assert not os.path.exists(j)
    assert sha(path) == before


def test_corrupt_crc_ignored(path, monkeypatch):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(0, b"QQQQ")
    monkeypatch.setattr(RawFile, "_apply_ranges",
                        lambda self, r: (_ for _ in ()).throw(RuntimeError()))
    kill_restore(monkeypatch)
    with pytest.raises(RuntimeError):
        rf.commit()
    monkeypatch.undo()
    rf.close()
    j = str(path) + ".journal"
    raw = bytearray(open(j, "rb").read())
    raw[12] ^= 0xFF  # damage the body, trailer intact
    open(j, "wb").write(bytes(raw))
    with pytest.raises(CkdError, match="journal corrupt"):
        RawFile.recover(path)
    assert os.path.exists(j)  # left alone


def test_growth_rolled_back(path, monkeypatch):
    before = sha(path)
    rf = RawFile(path, True)
    rf.begin()
    rf.write(len(ORIG) - 2, b"WXYZ")  # straddles EOF
    rf.write(len(ORIG) + 1000, b"far")

    def partial(self, ranges):
        off, data = ranges[0]
        os.pwrite(self._fd, data, off)
        raise RuntimeError("crash")

    monkeypatch.setattr(RawFile, "_apply_ranges", partial)
    kill_restore(monkeypatch)
    with pytest.raises(RuntimeError):
        rf.commit()
    monkeypatch.undo()
    rf.close()
    assert path.stat().st_size > len(ORIG)
    assert RawFile.recover(path) is True
    assert path.stat().st_size == len(ORIG)
    assert sha(path) == before


def test_journal_holds_original_bytes(path, monkeypatch):
    import struct
    rf = RawFile(path, True)
    rf.begin()
    rf.write(len(ORIG) - 2, b"WXYZ")
    monkeypatch.setattr(RawFile, "_apply_ranges",
                        lambda self, r: (_ for _ in ()).throw(RuntimeError()))
    kill_restore(monkeypatch)
    with pytest.raises(RuntimeError):
        rf.commit()
    monkeypatch.undo()
    rf.close()
    j = open(str(path) + ".journal", "rb").read()
    assert j[:8] == b"CKDOJNL1"
    assert struct.unpack(">Q", j[8:16])[0] == len(ORIG)
    off, ln = struct.unpack(">QI", j[16:28])
    assert (off, ln) == (len(ORIG) - 2, 2)  # only the part that existed
    assert j[28:30] == ORIG[-2:]
    assert j[30:34] == b"END!"


def damaged_rf(path, monkeypatch, n=4):
    rf = RawFile(path, True)
    rf.begin()
    for i in range(n):
        rf.write(i * 300, bytes([0xAB]) * 50)
    rf.write(len(ORIG) + 10, b"grow")

    def bad_apply(self, ranges):
        for off, data in ranges[:2]:
            os.pwrite(self._fd, data, off)
        raise RuntimeError("apply failed")

    monkeypatch.setattr(RawFile, "_apply_ranges", bad_apply)
    return rf


def test_apply_failure_rolls_back_in_process(path, monkeypatch):
    before = sha(path)
    rf = damaged_rf(path, monkeypatch)
    with pytest.raises(RuntimeError, match="apply failed"):
        rf.commit()
    assert sha(path) == before
    assert not os.path.exists(str(path) + ".journal")
    assert not rf.in_transaction
    assert rf.size == len(ORIG)
    monkeypatch.undo()
    rf.begin()  # reusable
    rf.write(0, b"ok")
    rf.commit()
    assert path.read_bytes()[:2] == b"ok"


def test_restore_failure_poisons_and_keeps_journal(path, monkeypatch):
    before = sha(path)
    rf = damaged_rf(path, monkeypatch)
    kill_restore(monkeypatch)
    with pytest.raises(RuntimeError):
        rf.commit()
    assert os.path.exists(str(path) + ".journal")
    for fn in (rf.begin, lambda: rf.write(0, b"x"), rf.commit):
        with pytest.raises(CkdError, match="needs recovery"):
            fn()
    monkeypatch.undo()
    rf.close()
    assert RawFile.recover(path) is True
    assert sha(path) == before


def test_begin_refuses_existing_journal(path):
    open(str(path) + ".journal", "wb").write(b"whatever")
    rf = RawFile(path, True)
    with pytest.raises(CkdError):
        rf.begin()


def test_read_negative_raises(path):
    rf = RawFile(path, False)
    with pytest.raises(ValueError):
        rf.read(-1, 4)
    with pytest.raises(ValueError):
        rf.read(0, -1)


def test_touching_writes(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(10, b"aa")
    rf.write(12, b"bb")
    rf.write(8, b"cc")
    assert rf._pending == [(8, b"ccaabb")]
    rf.commit()
    assert path.read_bytes()[8:14] == b"ccaabb"


def test_write_in_gap_before_pending_past_eof(path):
    n = len(ORIG)
    rf = RawFile(path, True)
    rf.begin()
    rf.write(n + 100, b"late")
    rf.write(n + 10, b"early")
    assert rf._pending == [(n + 10, b"early"), (n + 100, b"late")]
    assert rf.read(n + 8, 10) == b"\0\0early\0\0\0"
    rf.commit()
    d = path.read_bytes()
    assert d[n + 10:n + 15] == b"early" and d[n + 100:] == b"late"
    assert d[n:n + 10] == bytes(10)


# -- truncation (Task 5: CCKD trailing free space / free table at EOF) -------

def test_truncate_shrinks_on_commit(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.write(100, b"keep")
    rf.write(3000, b"gone")
    rf.truncate(2000)
    assert rf.size == 2000
    assert rf.read(1990, 50) == ORIG[1990:2000]
    rf.commit()
    assert rf.size == 2000
    rf.close()
    d = path.read_bytes()
    assert len(d) == 2000
    assert d[100:104] == b"keep" and d[:100] == ORIG[:100]


def test_truncate_then_grow_reads_zeros(path):
    rf = RawFile(path, True)
    rf.begin()
    rf.truncate(1000)
    rf.write(1500, b"new")
    assert rf.size == 1503
    assert rf.read(990, 20) == ORIG[990:1000] + bytes(10)
    rf.commit()
    rf.close()
    d = path.read_bytes()
    assert d[:1000] == ORIG[:1000]
    assert d[1000:1500] == bytes(500)
    assert d[1500:] == b"new"


def test_truncate_rollback(path):
    before = sha(path)
    rf = RawFile(path, True)
    rf.begin()
    rf.truncate(10)
    rf.rollback()
    assert rf.size == len(ORIG)
    assert rf.read(0, len(ORIG)) == ORIG
    rf.close()
    assert sha(path) == before


def test_truncate_misuse(path):
    rf = RawFile(path, True)
    with pytest.raises(CkdError):
        rf.truncate(10)  # outside a transaction
    rf.begin()
    with pytest.raises(CkdError):
        rf.truncate(len(ORIG) + 1)  # only shrinks
    with pytest.raises(CkdError):
        rf.truncate(-1)


def test_truncate_crash_recovers_tail(path, monkeypatch):
    before = sha(path)
    rf = RawFile(path, True)
    rf.begin()
    rf.write(0, b"head")
    rf.truncate(1000)

    def crash(self, ranges):
        raise RuntimeError("crash")

    monkeypatch.setattr(RawFile, "_apply_ranges", crash)
    kill_restore(monkeypatch)
    with pytest.raises(RuntimeError):
        rf.commit()
    monkeypatch.undo()
    rf.close()
    assert path.stat().st_size == 1000  # truncated before the crash
    assert RawFile.recover(path) is True
    assert sha(path) == before


def test_truncate_apply_failure_rolls_back_in_process(path, monkeypatch):
    before = sha(path)
    rf = RawFile(path, True)
    rf.begin()
    rf.write(5, b"x")
    rf.truncate(100)
    monkeypatch.setattr(RawFile, "_apply_ranges",
                        lambda self, r: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        rf.commit()
    assert sha(path) == before
    assert rf.size == len(ORIG)
