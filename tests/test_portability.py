"""The I/O layer works without POSIX-only APIs (as on Windows): no os.pread/os.pwrite,
no fcntl, no directory fsync. The whole stack runs in that mode on this machine."""

import hashlib
import os

import pytest

from ckdoffline.image import rawfile
from ckdoffline.image.rawfile import RawFile
from ckdoffline.session import Session
from oracle import cckdcdsk, dasdcat

FB = "TEST.FB80.PDS"


@pytest.fixture
def windows_like(monkeypatch):
    monkeypatch.delattr(os, "pread")
    monkeypatch.delattr(os, "pwrite")
    monkeypatch.setattr(rawfile, "fcntl", None)
    monkeypatch.setattr(rawfile, "WINDOWS", True)


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_rawfile_without_pread_pwrite(tmp_path, windows_like):
    p = tmp_path / "f.bin"
    p.write_bytes(bytes(range(256)) * 4)
    rf = RawFile(p, True)
    try:
        assert rf.read(250, 10) == bytes(range(250, 256)) + bytes(range(4))
        rf.begin()
        rf.write(1020, b"\xff" * 10)   # grows the file past EOF
        rf.write(5, b"\x0d\x0a\x1a")   # bytes text mode would mangle
        rf.commit()
        rf.begin()
        rf.truncate(1024)
        rf.commit()
    finally:
        rf.close()
    data = p.read_bytes()
    assert len(data) == 1024
    assert data[5:8] == b"\x0d\x0a\x1a" and data[1020:] == b"\xff" * 4


def test_crash_recovery_without_pread_pwrite(tmp_path, windows_like, monkeypatch):
    p = tmp_path / "f.bin"
    p.write_bytes(b"A" * 4096)
    before = _sha(p)
    rf = RawFile(p, True)
    rf.begin()
    rf.write(100, b"B" * 100)
    rf.write(5000, b"C" * 10)

    def die(self, ranges):
        rawfile._pwrite(self._fd, ranges[0][1], ranges[0][0])
        raise KeyboardInterrupt

    monkeypatch.setattr(RawFile, "_apply_ranges", die)
    monkeypatch.setattr(RawFile, "_restore", staticmethod(lambda *a: (_ for _ in ()).throw(OSError("dead"))))
    with pytest.raises(OSError):
        rf.commit()
    os.close(rf._fd)
    assert _sha(p) != before
    monkeypatch.undo()
    monkeypatch.delattr(os, "pread")
    monkeypatch.delattr(os, "pwrite")
    assert RawFile.recover(p) is True
    assert _sha(p) == before


def test_session_save_without_posix_io(fixture_images, windows_like):
    img = fixture_images["cckd_zlib"]
    s = Session.open(img, backup=False)
    try:
        s.save_text(FB, "WINMEM", ["HELLO FROM A WINDOWS-LIKE IO LAYER", "LINE TWO"])
        assert s.read_text(FB, "WINMEM")["lines"][0].rstrip() == "HELLO FROM A WINDOWS-LIKE IO LAYER"
    finally:
        s.close()
    assert cckdcdsk(img)[0] == 0
    out = dasdcat(img, FB, "WINMEM")
    assert out[:80].rstrip() == b"HELLO FROM A WINDOWS-LIKE IO LAYER"
    assert out[80:160].rstrip() == b"LINE TWO"
