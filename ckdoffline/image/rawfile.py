"""Byte-range overlay over a file, with an undo-journal transaction.

Writes are held in memory as pending ranges, plus an optional pending
truncation (truncate() shrinks the logical file). commit() first saves the
ORIGINAL bytes of every pending range and of any tail cut off by a truncation to ``<path>.journal`` (fsynced), then applies the
writes in place, then deletes the journal. If the process dies mid-apply,
recover() puts the original bytes back and truncates to the original size, so
an image is either fully saved or restored byte for byte.

Journal format (all integers big-endian):
    b"CKDOJNL1", u64 original file size,
    repeated (u64 offset, u32 length, <length> original bytes),
    b"END!", u32 CRC32 of everything before the b"END!".
A journal with no complete trailer was never completely written, so the apply
step never started and recover() just deletes it. A journal with a trailer but
a bad CRC is damaged: recover() refuses and raises CkdError.
"""

import os
import struct
import zlib
from pathlib import Path

from ckdoffline.errors import CkdError, ReadOnlyError

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

MAGIC = b"CKDOJNL1"
TRAILER = b"END!"

# Windows opens files in text mode unless O_BINARY is given; image bytes must
# never be translated. The flag doesn't exist (and isn't needed) elsewhere.
O_BINARY = getattr(os, "O_BINARY", 0)
WINDOWS = os.name == "nt"


def _pread(fd: int, n: int, off: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, n, off)
    # Windows has no pread: seek + read. Each RawFile owns its fd and is used
    # from one thread at a time (the server serializes requests), so the shared
    # file position is safe.
    os.lseek(fd, off, os.SEEK_SET)
    chunks = []
    while n > 0:
        b = os.read(fd, n)
        if not b:
            break
        chunks.append(b)
        n -= len(b)
    return b"".join(chunks)


def _pwrite(fd: int, data: bytes, off: int) -> None:
    view = memoryview(data)
    if hasattr(os, "pwrite"):
        while view:
            n = os.pwrite(fd, view, off)
            view, off = view[n:], off + n
        return
    os.lseek(fd, off, os.SEEK_SET)  # Windows: see _pread
    while view:
        view = view[os.write(fd, view):]


def _fsync(fd: int) -> None:
    """fsync; on macOS prefer F_FULLFSYNC (flush the drive cache) if it works."""
    full = getattr(fcntl, "F_FULLFSYNC", None) if fcntl else None
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    """Make a create/delete in the image's directory durable. Windows can't open
    a directory as a file; NTFS journals directory changes itself."""
    if WINDOWS:
        return
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        _fsync(fd)
    finally:
        os.close(fd)


def _journal_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".journal")


class RawFile:
    def __init__(self, path: Path, writable: bool):
        self.path = Path(path)
        self.writable = writable
        # Unbuffered fd + pread/pwrite: no stale buffered bytes can ever be
        # flushed over a restore.
        self._fd = os.open(self.path, (os.O_RDWR if writable else os.O_RDONLY) | O_BINARY)
        self._disk_size = os.fstat(self._fd).st_size
        self._poisoned = False
        self._pending: list[tuple[int, bytes]] = []  # sorted, non-overlapping
        self._in_txn = False
        self._reset_size()

    def _reset_size(self) -> None:
        # _size: logical size (growth and truncation included).
        # _cut: on-disk bytes at or after this offset are gone (truncated);
        # anything later re-grown there reads as zeros.
        self._size = self._disk_size
        self._cut = self._disk_size

    # -- state -------------------------------------------------------------
    @property
    def in_transaction(self) -> bool:
        return self._in_txn

    @property
    def size(self) -> int:
        """File size including pending growth and truncation."""
        return self._size

    def close(self) -> None:
        os.close(self._fd)

    def refresh_size(self) -> None:
        """Re-read the on-disk size (someone else may have grown or shrunk the file).
        Only between transactions."""
        if self._in_txn:
            raise CkdError("cannot refresh the file size inside a transaction")
        self._disk_size = os.fstat(self._fd).st_size
        self._reset_size()

    # -- reading -----------------------------------------------------------
    def read(self, off: int, n: int) -> bytes:
        """Read up to n bytes at off (short only at EOF), including pending writes."""
        if off < 0 or n < 0:
            raise ValueError("negative offset or length")
        end = min(off + n, self.size)
        if end <= off:
            return b""
        buf = bytearray(end - off)  # gaps past the on-disk EOF read as zeros
        if off < self._cut:
            disk = _pread(self._fd, min(end, self._cut) - off, off)
            buf[:len(disk)] = disk
        for poff, pdata in self._pending:
            lo, hi = max(poff, off), min(poff + len(pdata), end)
            if lo < hi:
                buf[lo - off:hi - off] = pdata[lo - poff:hi - poff]
        return bytes(buf)

    # -- transaction -------------------------------------------------------
    def _check_usable(self) -> None:
        if self._poisoned:
            raise CkdError("image needs recovery")

    def begin(self) -> None:
        if not self.writable:
            raise ReadOnlyError("image is read-only")
        self._check_usable()
        if _journal_path(self.path).exists():
            raise CkdError("a journal exists for this image; recovery required")
        if self._in_txn:
            raise CkdError("transaction already in progress")
        self._in_txn = True

    def write(self, off: int, data: bytes) -> None:
        """Record a pending write; later writes win where ranges overlap."""
        if not self.writable:
            raise ReadOnlyError("image is read-only")
        self._check_usable()
        if not self._in_txn:
            raise CkdError("write outside a transaction")
        if off < 0:
            raise CkdError("negative write offset")
        data = bytes(data)
        if not data:
            return
        lo, hi = off, off + len(data)
        before, after = [], []
        head = tail = b""
        for poff, pdata in self._pending:
            pend = poff + len(pdata)
            if pend < off:
                before.append((poff, pdata))
            elif poff > off + len(data):
                after.append((poff, pdata))
            else:  # overlapping or adjacent: merge into the new range
                if poff < lo:
                    lo, head = poff, pdata[:off - poff]
                if pend > hi:
                    hi, tail = pend, pdata[off + len(data) - poff:]
        self._pending = before + [(lo, head + data + tail)] + after
        self._size = max(self._size, off + len(data))

    def truncate(self, size: int) -> None:
        """Record a pending shrink of the file to ``size`` bytes."""
        if not self.writable:
            raise ReadOnlyError("image is read-only")
        self._check_usable()
        if not self._in_txn:
            raise CkdError("truncate outside a transaction")
        if not 0 <= size <= self._size:
            raise CkdError(f"cannot truncate a {self._size}-byte file to {size} bytes")
        self._pending = [(off, data[:size - off]) for off, data in self._pending if off < size]
        self._size = size
        self._cut = min(self._cut, size)

    def rollback(self) -> None:
        self._pending = []
        self._in_txn = False
        self._reset_size()

    def commit(self) -> None:
        self._check_usable()
        if not self._in_txn:
            raise CkdError("commit outside a transaction")
        if not self._pending and self._size == self._disk_size:
            self._in_txn = False
            return
        ranges = list(self._pending)
        cut, new_size = self._cut, self._size
        jpath = _journal_path(self.path)
        if jpath.exists():
            raise CkdError("a journal exists for this image; recovery required")
        items = []
        body = bytearray(MAGIC + struct.pack(">Q", self._disk_size))
        for off, data in ranges:
            orig = b""
            if off < self._disk_size:  # only the part that exists today
                orig = _pread(self._fd, min(len(data), self._disk_size - off), off)
            items.append((off, orig))
            body += struct.pack(">QI", off, len(orig)) + orig
        if cut < self._disk_size:  # the tail a truncation removes
            orig = _pread(self._fd, self._disk_size - cut, cut)
            items.append((cut, orig))
            body += struct.pack(">QI", cut, len(orig)) + orig
        body += TRAILER
        body += struct.pack(">I", zlib.crc32(bytes(body[:-4])))
        with open(jpath, "wb") as jf:
            jf.write(body)
            jf.flush()
            _fsync(jf.fileno())
        _fsync_dir(self.path)
        # A process death here leaves the journal for recover(). An exception
        # is rolled back in-process from the in-memory copy of the journal.
        orig_size = self._disk_size
        try:
            if cut < orig_size:
                os.ftruncate(self._fd, cut)
            self._apply_ranges(ranges)
            if os.fstat(self._fd).st_size != new_size:
                os.ftruncate(self._fd, new_size)  # re-grown past the cut
            _fsync(self._fd)
        except BaseException:
            try:
                self._restore(self._fd, orig_size, items)
                jpath.unlink()
                _fsync_dir(self.path)
            except BaseException:
                self._poisoned = True  # journal stays on disk
                raise
            self._disk_size = orig_size
            self._pending = []
            self._in_txn = False
            self._reset_size()
            raise
        jpath.unlink()
        _fsync_dir(self.path)
        self._disk_size = os.fstat(self._fd).st_size
        self._pending = []
        self._in_txn = False
        self._reset_size()

    def _apply_ranges(self, ranges: list[tuple[int, bytes]]) -> None:
        """Write the ranges into the file in place.

        NOTE: tests/test_rawfile.py simulates a crash by monkeypatching this
        method (it receives the sorted list of (offset, bytes) and may use
        self._fd via _pwrite), so keep the name and signature stable.
        """
        for off, data in ranges:
            _pwrite(self._fd, data, off)  # writing past EOF zero-fills the gap

    @staticmethod
    def _restore(fd: int, orig_size: int, items) -> None:
        """Write original bytes back, truncate to orig_size, fsync."""
        for off, orig in items:
            if orig:
                _pwrite(fd, orig, off)
        os.ftruncate(fd, orig_size)
        _fsync(fd)

    # -- recovery ----------------------------------------------------------
    @staticmethod
    def recover(path: Path) -> bool:
        """Restore the image from ``<path>.journal`` if one exists.

        Returns True if a valid journal was replayed. A journal with a missing or
        incomplete trailer was never fully written (apply had not started): it
        is deleted, nothing else changes, and False is returned. A journal with
        a trailer but a bad CRC raises CkdError and is left alone.
        """
        path = Path(path)
        jpath = _journal_path(path)
        if not jpath.exists():
            return False
        parsed = RawFile._parse_journal(jpath.read_bytes())
        if parsed is None:
            jpath.unlink()
            _fsync_dir(path)
            return False
        orig_size, items = parsed
        fd = os.open(path, os.O_RDWR | O_BINARY)
        try:
            RawFile._restore(fd, orig_size, items)
        finally:
            os.close(fd)
        jpath.unlink()
        _fsync_dir(path)
        return True

    @staticmethod
    def _parse_journal(raw: bytes):
        """Return (original_size, items), None if no complete trailer.

        Raises CkdError if the trailer is present but the journal is damaged.
        """
        if len(raw) < 24 or raw[:8] != MAGIC or raw[-8:-4] != TRAILER:
            return None
        corrupt = CkdError("journal corrupt; image may be half-written "
                           "— restore from backup")
        if struct.unpack(">I", raw[-4:])[0] != zlib.crc32(raw[:-8]):
            raise corrupt
        orig_size = struct.unpack(">Q", raw[8:16])[0]
        items, pos, end = [], 16, len(raw) - 8
        while pos < end:
            if pos + 12 > end:
                raise corrupt
            off, ln = struct.unpack(">QI", raw[pos:pos + 12])
            pos += 12
            if pos + ln > end:
                raise corrupt
            items.append((off, raw[pos:pos + ln]))
            pos += ln
        return orig_size, items
