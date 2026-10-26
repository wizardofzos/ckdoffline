"""CCKD (CKD_C370) and CCKD64 (CKD_C064): L1/L2 tables, per-track compression."""

import bz2
import struct
import zlib

from ckdoffline.errors import FormatError, UnsupportedError
from ckdoffline.image.base import ENDTRK, TRKHDR_SIZE, DasdImage, trim_track
from ckdoffline.image.cckdspace import (CDH_POS, CDH_SIZE, L1_POS, NULLFMT_MAX, OPT_OPENRW,
                                        CckdHeader, Layout, SpaceMap, check_space)

OPT_BIGEND = 0x02
OPT_OPENED = 0x80
NULL_FMT2_DLEN = 4096


class CckdImage(DasdImage):
    def __init__(self, path, raw, cyls, heads, trksize, *, is64, nullfmt, num_l1, opened):
        super().__init__(path, raw, cyls, heads, trksize)
        self.fmt = "CCKD64" if is64 else "CCKD"
        self.opened_flag = opened
        self._is64 = is64
        self._nullfmt = nullfmt
        self._num_l1 = num_l1
        self._lay = Layout(is64)  # all entry widths and formats
        self._l1 = self._load_l1()
        self._l2_cache: dict[int, bytes] = {}
        self._space: SpaceMap | None = None  # free space of the open transaction

    @classmethod
    def from_header(cls, path, raw, magic, hdr, heads, trksize):
        is64 = magic == b"CKD_C064"
        cdh = raw.read(CDH_POS, CDH_SIZE)
        if len(cdh) < CDH_SIZE:
            raise FormatError("compressed device header is truncated")
        opts = cdh[3]
        if opts & OPT_BIGEND:
            raise UnsupportedError("big-endian CCKD images are not supported")
        num_l1 = struct.unpack_from("<i", cdh, 4)[0]
        if is64:
            cyls = struct.unpack_from("<I", cdh, 12)[0]
            nullfmt = cdh[72]
        else:
            cyls = struct.unpack_from("<I", cdh, 40)[0]
            nullfmt = cdh[44]
        if nullfmt > NULLFMT_MAX:
            nullfmt = 0  # as Hercules does on open
        if cyls == 0 or num_l1 <= 0 or num_l1 < (cyls * heads + 255) // 256:
            raise FormatError("invalid compressed device header")
        return cls(path, raw, cyls, heads, trksize, is64=is64, nullfmt=nullfmt,
                   num_l1=num_l1, opened=bool(opts & OPT_OPENED))

    def disk_opened_flag(self) -> bool:
        cdh = self.raw.read(CDH_POS, CDH_SIZE)
        return len(cdh) == CDH_SIZE and bool(cdh[3] & OPT_OPENED)

    def _load_l1(self) -> list[int]:
        lay = self._lay
        data = self.raw.read(L1_POS, self._num_l1 * lay.l1_size)
        if len(data) < self._num_l1 * lay.l1_size:
            raise FormatError("L1 table is truncated")
        return [v[0] for v in struct.iter_unpack(lay.l1_fmt, data)]

    def _invalidate(self, trk: int | None = None) -> None:
        """Drop cached tracks and table caches; a full invalidate (commit,
        rollback) also reloads L1, which may have been rewritten."""
        super()._invalidate(trk)
        if trk is None:
            self._l2_cache.clear()
        else:
            self._l2_cache.pop(trk >> 8, None)
        self._l1 = self._load_l1()  # L1 may have changed (new L2 table)

    def _l2_entry(self, trk: int):
        """(trkoff, len, size) for the track; (0, nullfmt-or-len, 0) when null."""
        l1x, l2x = trk >> 8, trk & 0xFF
        if l1x >= len(self._l1):
            raise FormatError(f"track {trk} is beyond the L1 table")
        l1, lay = self._l1[l1x], self._lay
        if lay.is_null(l1):
            return 0, self._nullfmt, 0
        tab = self._l2_cache.get(l1x)
        if tab is None:
            tab = self.raw.read(l1, lay.l2tab_size)
            if len(tab) < lay.l2tab_size:
                raise FormatError(f"L2 table for track {trk} is truncated")
            self._l2_cache[l1x] = tab
        ent = struct.unpack_from(lay.l2_fmt, tab, l2x * lay.l2_size)
        return ent[0], ent[1], ent[2]

    def _read_track_uncached(self, trk: int) -> bytes:
        off, length, _size = self._l2_entry(trk)
        if self._lay.is_null(off):
            return self._null_track(trk, length)
        stored = self.raw.read(off, length)
        if len(stored) < length or length < TRKHDR_SIZE:
            raise FormatError(f"track {trk} image is truncated")
        comp = stored[0]
        # cckd_uncompress_*: the first CKD_TRKHDR_SIZE (5) bytes are stored
        # uncompressed; the remainder is the compressed stream. Byte 0 is
        # normalised to 0 after uncompressing.
        exact = True  # compressed images must end exactly at the marker
        if comp == 0:
            body = stored[TRKHDR_SIZE:]
            exact = False  # may carry padding inside its stored length
        elif comp == 1:
            try:
                body = zlib.decompress(stored[TRKHDR_SIZE:])
            except zlib.error as e:
                raise FormatError(f"track {trk}: zlib error: {e}") from None
        elif comp == 2:
            try:
                body = bz2.decompress(stored[TRKHDR_SIZE:])
            except (OSError, ValueError) as e:
                raise FormatError(f"track {trk}: bzip2 error: {e}") from None
        else:
            raise FormatError(f"track {trk}: unknown compression code {comp}")
        img = b"\x00" + stored[1:TRKHDR_SIZE] + body
        if exact:
            if not img.endswith(ENDTRK):
                raise FormatError(f"track {trk}: no end-of-track marker")
        else:
            img = trim_track(img, f"track {trk}")
        want = bytes([0, trk // self.heads >> 8, trk // self.heads & 255,
                      trk % self.heads >> 8, trk % self.heads & 255])
        if img[:TRKHDR_SIZE] != want:
            raise FormatError(f"track {trk}: header CCHH does not match its position")
        return img

    # -- writing ------------------------------------------------------------
    def check_space(self) -> list[tuple[int, int]]:
        """Verify the file's space accounting (cckdcdsk's rules); returns the
        free blocks, raises FormatError if anything is inconsistent."""
        hdr = CckdHeader(self.raw.read(CDH_POS, CDH_SIZE), self._lay)
        return check_space(self.raw, hdr, self._lay, self._load_l1(), self.trksize)

    def _txn_space(self) -> SpaceMap:
        if self._space is None:
            # Refuse to allocate in a file whose accounting is already off.
            hdr = CckdHeader(self.raw.read(CDH_POS, CDH_SIZE), self._lay)
            free = check_space(self.raw, hdr, self._lay, self._l1, self.trksize)
            hdr.opts |= OPT_OPENRW  # as Hercules does when it opens R/W
            self._space = SpaceMap(self.raw, hdr, self._lay, free, self._l1)
        return self._space

    def write_track(self, cyl: int, head: int, img: bytes) -> None:
        """cckd_write_trkimg: store the image (zlib, or uncompressed when that
        is not smaller), point the L2 entry at it, release the old space."""
        trk = self._trk(cyl, head)
        self._require_txn()
        img = self._check_track_image(trk, img)
        stored = _compress_zlib(img)
        space = self._txn_space()
        lay = self._lay
        l1x, l2x = trk >> 8, trk & 0xFF
        l2off = self._l1[l1x]
        old = None if lay.is_null(l2off) else self._l2_entry(trk)
        # From here on, pending bytes and space totals change step by step: a
        # failure part-way poisons the transaction (only rollback() then).
        with self._mutating():
            off = space.get(len(stored))
            self.raw.write(off, stored)
            entry = lay.pack_l2(off, len(stored), len(stored))
            if old is None:
                # cckd_write_l2: a new table; the other 255 tracks stay the null
                # tracks they were (cckd_read_l2's null table for cdh_nullfmt).
                tab = bytearray(lay.pack_l2(0, self._nullfmt, self._nullfmt) * 256)
                tab[l2x * lay.l2_size:(l2x + 1) * lay.l2_size] = entry
                space.l2_bounds += lay.l2tab_size
                l2off = space.get(lay.l2tab_size, l2=True)
                self.raw.write(l2off, tab)
                self.raw.write(L1_POS + l1x * lay.l1_size, struct.pack(lay.l1_fmt, l2off))
            else:
                self.raw.write(l2off + l2x * lay.l2_size, entry)
                space.release(*old)
            self._invalidate(trk)

    def commit(self) -> None:
        self._check_not_poisoned()
        if self._space is not None:
            with self._mutating():
                self._space.finish()
        self._space = None
        super().commit()

    def rollback(self) -> None:
        self._space = None
        super().rollback()

    def _null_track(self, trk: int, nullfmt: int) -> bytes:
        """cckd_null_trk / cckd64_null_trk: ha r0 [eof | 12 x 4K] eot."""
        if nullfmt > NULLFMT_MAX:
            nullfmt = self._nullfmt
        elif nullfmt == 0 and self._nullfmt == 2:
            nullfmt = 2  # Hercules compatibility quirk for old Linux images
        cyl, head = divmod(trk, self.heads)
        ch = struct.pack(">HH", cyl, head)
        out = bytearray(b"\x00" + ch)
        out += ch + bytes([0, 0]) + struct.pack(">H", 8) + bytes(8)  # R0
        if nullfmt == 0:
            out += ch + bytes([1, 0]) + struct.pack(">H", 0)         # R1 EOF
        elif nullfmt == 2:
            for r in range(1, 13):
                out += ch + bytes([r, 0]) + struct.pack(">H", NULL_FMT2_DLEN)
                out += bytes(NULL_FMT2_DLEN)
        out += ENDTRK
        return bytes(out)


def _compress_zlib(img: bytes) -> bytes:
    """cckd_compress_zlib: the 5-byte HA stays uncompressed with byte 0 = 1;
    if that is not smaller, the image is stored as is (byte 0 = 0)."""
    body = zlib.compress(img[TRKHDR_SIZE:])
    if TRKHDR_SIZE + len(body) < len(img):
        return b"\x01" + img[1:TRKHDR_SIZE] + body
    return img
