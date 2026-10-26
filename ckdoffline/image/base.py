"""DasdImage interface and open_image() format dispatch."""

import struct
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from ckdoffline.errors import CkdError, FormatError, ReadOnlyError, UnsupportedError
from ckdoffline.image.rawfile import RawFile

DEVHDR_SIZE = 512
DEVTYPE_3390 = 0x90
TRKHDR_SIZE = 5
ENDTRK = b"\xff" * 8
CACHE_TRACKS = 256


def trim_track(img: bytes, what: str) -> bytes:
    """Walk the records from the HA to the FF*8 end-of-track marker and return
    the image up to and including it (cckd_trklen). FormatError if none."""
    pos = TRKHDR_SIZE
    n = len(img)
    while pos + 8 <= n:
        if img[pos:pos + 8] == ENDTRK:
            return img[:pos + 8]
        pos += 8 + img[pos + 5] + struct.unpack_from(">H", img, pos + 6)[0]
    raise FormatError(f"{what} has no end-of-track marker")


class DasdImage:
    """A 3390 image. Tracks are exchanged as uncompressed Hercules track images:
    HA (00 CC CC HH HH), R0 count + data, records, FF*8 end-of-track."""

    fmt = ""

    def __init__(self, path: Path, raw: RawFile, cyls: int, heads: int, trksize: int):
        self.path = Path(path)
        self.raw = raw
        self.writable = raw.writable
        self.devtype = 0x3390
        self.cyls = cyls
        self.heads = heads
        self.trksize = trksize
        self.opened_flag = False
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_max = CACHE_TRACKS
        self._txn_error: str | None = None  # set when a write failed half-way

    # -- transaction passthrough ------------------------------------------
    @contextmanager
    def _mutating(self):
        """Wrap code that changes pending state: if it fails part-way the
        transaction is poisoned and only rollback() is accepted."""
        try:
            yield
        except BaseException as e:
            self._txn_error = str(e) or type(e).__name__
            raise

    def mutating(self):
        """Public form of _mutating for multi-step changes made above the
        image layer (extend + track writes + DSCB writes): any exception
        inside it poisons the transaction, so commit() refuses and only
        rollback() is accepted."""
        return self._mutating()

    def _check_not_poisoned(self) -> None:
        if self._txn_error is not None:
            raise CkdError(f"transaction aborted: {self._txn_error}; roll it back")

    def begin(self) -> None:
        self.raw.begin()

    def commit(self) -> None:
        self._check_not_poisoned()
        try:
            self.raw.commit()
        finally:
            self._invalidate()  # cached tracks may predate the committed writes

    def rollback(self) -> None:
        self._txn_error = None
        self.raw.rollback()
        self._invalidate()

    def close(self) -> None:
        self.raw.close()

    def drop_caches(self) -> None:
        """Forget every cached track and table and the file size (the file was changed by
        someone else). Only between transactions."""
        self.raw.refresh_size()
        self._invalidate()

    def disk_opened_flag(self) -> bool:
        """The in-use flag as it is on disk now (plain CKD has none)."""
        return False

    # -- tracks -----------------------------------------------------------
    def _trk(self, cyl: int, head: int) -> int:
        if not (0 <= cyl < self.cyls and 0 <= head < self.heads):
            raise FormatError(f"track {cyl}/{head} is outside the volume "
                              f"({self.cyls} cylinders, {self.heads} heads)")
        return cyl * self.heads + head

    def read_track(self, cyl: int, head: int) -> bytes:
        trk = self._trk(cyl, head)
        hit = self._cache.get(trk)
        if hit is not None:
            self._cache.move_to_end(trk)
            return hit
        img = self._read_track_uncached(trk)
        self._cache[trk] = img
        if len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return img

    def _read_track_uncached(self, trk: int) -> bytes:
        raise NotImplementedError

    def write_track(self, cyl: int, head: int, img: bytes) -> None:
        """Replace a track inside begin()/commit(). ``img`` is a complete
        track image (same convention as read_track), at most trksize bytes."""
        raise NotImplementedError

    def _require_txn(self) -> None:
        if not self.writable:
            raise ReadOnlyError("image is read-only")
        if not self.raw.in_transaction:
            raise CkdError("track write outside a transaction")
        self._check_not_poisoned()

    def _check_track_image(self, trk: int, img) -> bytes:
        """Validate a new track image the way cckdcdsk level 3 does
        (cdsk_valid_trk) and return it as bytes; FormatError if invalid."""
        img = bytes(img)
        cyl, head = divmod(trk, self.heads)
        what = f"new image for track {cyl}/{head}"
        if len(img) > self.trksize:
            raise FormatError(f"{what} is {len(img)} bytes; the track size is {self.trksize}")
        cchh = struct.pack(">HH", cyl, head)
        if img[:TRKHDR_SIZE] != b"\x00" + cchh:
            raise FormatError(f"{what}: home address does not match the track")
        if img[TRKHDR_SIZE:TRKHDR_SIZE + 8] != cchh + b"\x00\x00\x00\x08":
            raise FormatError(f"{what}: invalid R0 count field")
        pos, n = TRKHDR_SIZE + 16, len(img)
        while pos + 8 <= n and img[pos:pos + 8] != ENDTRK:
            if img[pos + 4] == 0 or struct.unpack_from(">H", img, pos + 2)[0] >= self.heads:
                raise FormatError(f"{what}: invalid count field at offset {pos}")
            pos += 8 + img[pos + 5] + struct.unpack_from(">H", img, pos + 6)[0]
        if img[pos:pos + 8] != ENDTRK or pos + 8 != n:
            raise FormatError(f"{what} does not end with the end-of-track marker")
        return img

    def _invalidate(self, trk: int | None = None) -> None:
        """Drop cached tracks (all, or one). write_track must call this."""
        if trk is None:
            self._cache.clear()
        else:
            self._cache.pop(trk, None)


def open_image(path: Path, writable: bool) -> DasdImage:
    from ckdoffline.image.cckd import CckdImage
    from ckdoffline.image.ckd import CkdImage

    raw = RawFile(Path(path), writable)
    try:
        hdr = raw.read(0, DEVHDR_SIZE)
        if len(hdr) < DEVHDR_SIZE:
            raise FormatError("file is too small to be a DASD image")
        magic = hdr[:8]
        if magic in (b"CKD_P370", b"CKD_P064"):
            cls = CkdImage
        elif magic in (b"CKD_C370", b"CKD_C064"):
            cls = CckdImage
        else:
            raise FormatError("not a CKD/CCKD image (unrecognised header)")
        if hdr[16] != DEVTYPE_3390:
            raise UnsupportedError(f"device type X'{0x3300 | hdr[16]:04X}' is not supported; only 3390")
        heads, trksize = struct.unpack_from("<II", hdr, 8)
        if heads == 0 or trksize < TRKHDR_SIZE + len(ENDTRK):
            raise FormatError("invalid device header geometry")
        return cls.from_header(Path(path), raw, magic, hdr, heads, trksize)
    except BaseException:
        raw.close()
        raise
