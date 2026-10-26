"""Plain CKD (CKD_P370 / CKD_P064): fixed trksize slots after the 512-byte header."""

import struct

from ckdoffline.errors import FormatError, UnsupportedError
from ckdoffline.image.base import DEVHDR_SIZE, DasdImage, trim_track


class CkdImage(DasdImage):
    fmt = "CKD"

    @classmethod
    def from_header(cls, path, raw, magic, hdr, heads, trksize):
        # ckddasd.c: slot size is exactly dh_trksize; cyls from the file size.
        trks = (raw.size - DEVHDR_SIZE) // trksize
        cyls = trks // heads
        if cyls == 0:
            raise FormatError("image contains no complete cylinder")
        # ckddasd.c: dh_fileseq (byte 17) and dh_highcyl (bytes 18-19, LE) mark one file of a
        # split image. A single file has highcyl 0 (fileseq 0 or 1), or fileseq 1 with
        # highcyl equal to its cylinder count (which Hercules also treats as complete).
        fileseq, highcyl = hdr[17], struct.unpack_from("<H", hdr, 18)[0]
        single = (highcyl == 0 and fileseq in (0, 1)) or (fileseq == 1 and highcyl == cyls)
        if not single:
            raise UnsupportedError("split CKD images are not supported")
        return cls(path, raw, cyls, heads, trksize)

    def _read_track_uncached(self, trk: int) -> bytes:
        slot = self.raw.read(DEVHDR_SIZE + trk * self.trksize, self.trksize)
        if len(slot) < self.trksize:
            raise FormatError(f"track {trk} is truncated")
        # The slot is padded after the end-of-track marker; trim so every
        # format yields identical bytes.
        return trim_track(slot, f"track {trk}")

    def write_track(self, cyl: int, head: int, img: bytes) -> None:
        trk = self._trk(cyl, head)
        self._require_txn()
        img = self._check_track_image(trk, img)
        with self._mutating():
            self.raw.write(DEVHDR_SIZE + trk * self.trksize, img + bytes(self.trksize - len(img)))
            self._invalidate(trk)
