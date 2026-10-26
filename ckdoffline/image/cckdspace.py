"""CCKD / CCKD64 file-space management, after Hyperion cckddasd.c and
cckddasd64.c (cckd_get_space, cckd_rel_space, cckd_flush_space,
cckd_read_fsp, cckd_write_fsp) and the rules cckdcdsk checks (cckdutil.c).

Free space on disk, current ("new") format, as cckd_write_fsp writes it:
free_off points to a table made of one FREEBLK-sized slot starting with the
8 bytes b"FREE_BLK", then free_num FREEBLK entries (offset of the free block,
its length), in ascending offset order. The table is stored inside the first
free block big enough to hold it; if none is, it is appended at cdh_size, so
the file is then longer than cdh_size (Hercules truncates it away the next
time it reads the free space). The "old" format is a chain through the free
blocks themselves: each block starts with (offset of the next block, length
of this one), free_off is the first block and the last link is 0. Both are
read. The new format is written; the old one only when the table cannot be
appended because the file is at its maximum size, as cckd_write_fsp does.

FREEBLK is (u32, u32) for CCKD and (u64, u64) for CCKD64, little-endian.

Differences from Hercules, all invisible on disk once a transaction commits:
released space is reusable at once (Hercules keeps it "pending" for a few
flushes because it writes in place; here a commit is all-or-nothing through
the undo journal), and adjacent free blocks are merged immediately rather
than at the next cckd_flush_space.
"""

import bisect
import struct

from ckdoffline.errors import FormatError, NoSpaceError

FREE_MAGIC = b"FREE_BLK"
CDH_POS = 512
CDH_SIZE = 512
L1_POS = 1024
OPT_OPENRW = 0x40
NULLFMT_MAX = 2          # L2 len/size <= 2 means a null track (no space)
TRKHDR_SIZE = 5


def max_file_size(is64: bool) -> int:
    """cckd_maxsize: CCKD_MAXSIZE / CCKD64_MAXSIZE."""
    return (1 << 64) - 1 if is64 else 0xFFFFFFFF


class Layout:
    """Entry widths for one format."""

    def __init__(self, is64: bool):
        self.is64 = is64
        self.l1_fmt, self.l1_size = ("<Q", 8) if is64 else ("<I", 4)
        self.l2_fmt, self.l2_size = ("<QHHI", 16) if is64 else ("<IHH", 8)
        self.fb_fmt, self.fb_size = ("<QQ", 16) if is64 else ("<II", 8)
        self.l2tab_size = 256 * self.l2_size
        self.all_ones = (1 << 64) - 1 if is64 else 0xFFFFFFFF
        # cdh_size, cdh_used, free_off, free_total, free_largest, free_num, free_imbed
        self.hdr_fmt, self.hdr_pos = ("<QQQQQqQ", 16) if is64 else ("<IIIIIiI", 12)

    def is_null(self, off: int) -> bool:
        return off == 0 or off == self.all_ones

    def l2_entries(self, tab: bytes):
        """(off, len, size) for each of the 256 entries of an L2 table."""
        return [e[:3] for e in struct.iter_unpack(self.l2_fmt, tab)]

    def pack_l2(self, off: int, length: int, size: int) -> bytes:
        return struct.pack(self.l2_fmt, off, length, size, *([0] if self.is64 else []))


class CckdHeader:
    """The space fields and options byte of the compressed device header."""

    def __init__(self, data: bytes, layout: Layout):
        if len(data) < CDH_SIZE:
            raise FormatError("compressed device header is truncated")
        self._data = bytes(data)
        self._layout = layout
        self.opts = data[3]
        (self.size, self.used, self.free_off, self.free_total, self.free_largest,
         self.free_num, self.free_imbed) = struct.unpack_from(layout.hdr_fmt, data, layout.hdr_pos)

    def pack(self) -> bytes:
        out = bytearray(self._data)
        out[3] = self.opts
        struct.pack_into(self._layout.hdr_fmt, out, self._layout.hdr_pos, self.size, self.used,
                         self.free_off, self.free_total, self.free_largest, self.free_num,
                         self.free_imbed)
        return bytes(out)


def read_free_list(raw, hdr: CckdHeader, lay: Layout) -> list[tuple[int, int]]:
    """The free blocks recorded at free_off (cckd_read_fsp), as (offset, length)."""
    if hdr.free_num <= 0 or hdr.free_off == 0:
        return []
    bad = FormatError("CCKD free space table is unreadable; run cckdcdsk on the image")
    first = raw.read(hdr.free_off, lay.fb_size)
    if len(first) < lay.fb_size:
        raise bad
    if first[:8] == FREE_MAGIC:
        n = hdr.free_num * lay.fb_size
        tab = raw.read(hdr.free_off + lay.fb_size, n)
        if len(tab) < n:
            raise bad
        return [tuple(e) for e in struct.iter_unpack(lay.fb_fmt, tab)]
    blocks, off = [], hdr.free_off
    for _ in range(hdr.free_num):
        fb = raw.read(off, lay.fb_size)
        if len(fb) < lay.fb_size:
            raise bad
        nxt, length = struct.unpack(lay.fb_fmt, fb)
        blocks.append((off, length))
        off = nxt
    if off != 0:
        raise bad
    return blocks


def check_space(raw, hdr: CckdHeader, lay: Layout, l1: list[int], trksize: int) -> list[tuple[int, int]]:
    """Rebuild the space map from the L1/L2 tables, as cckdcdsk does, and check
    that the header totals and the recorded free space match it exactly.
    Returns the free blocks; raises FormatError on any inconsistency."""
    def bad(detail):
        return FormatError(f"CCKD space accounting is inconsistent ({detail}); "
                           "run cckdcdsk on the image")

    items = [(0, 512, 512, False), (CDH_POS, CDH_SIZE, CDH_SIZE, False),
             (L1_POS, len(l1) * lay.l1_size, len(l1) * lay.l1_size, False)]
    for l1x, l2off in enumerate(l1):
        if lay.is_null(l2off):
            continue
        items.append((l2off, lay.l2tab_size, lay.l2tab_size, False))
        tab = raw.read(l2off, lay.l2tab_size)
        if len(tab) < lay.l2tab_size:
            raise bad(f"L2 table {l1x} is truncated")
        for l2x, (off, length, size) in enumerate(lay.l2_entries(tab)):
            if lay.is_null(off):
                continue
            if length < TRKHDR_SIZE or length > size or length > trksize:
                raise bad(f"track {l1x * 256 + l2x} has length {length}, size {size}")
            items.append((off, length, size, True))
    items.sort()

    if (hdr.free_off == 0) != (hdr.free_num == 0):
        raise bad(f"free_off is {hdr.free_off} but free_num is {hdr.free_num}")
    st_size = raw.size
    last_end = max(off + size for off, _len, size, _trk in items)
    if st_size == hdr.size:
        eof = st_size
    elif hdr.free_off == hdr.size and last_end == hdr.size < st_size:
        # New-format free space table appended at the end: cckdutil.c drops
        # the gap from cdh_size to EOF whatever its length (an older table
        # may have been longer); cckd_read_fsp truncates it away.
        eof = hdr.size
    else:
        raise bad(f"file size {st_size}, cdh_size {hdr.size}")

    gaps, used, imbed = [], 0, 0
    for k, (off, length, size, is_trk) in enumerate(items):
        nxt = items[k + 1][0] if k + 1 < len(items) else eof
        end = off + size
        if end > nxt:
            raise bad(f"space at offset {off} length {size} overlaps the next one at {nxt}")
        if end < nxt:
            gaps.append((end, nxt - end))
        used += length
        if is_trk:
            imbed += size - length
    want = dict(size=eof, used=used, free_total=sum(g for _, g in gaps) + imbed,
                free_imbed=imbed, free_num=len(gaps),
                free_largest=max((g for _, g in gaps), default=0))
    for name, value in want.items():
        if getattr(hdr, name) != value:
            raise bad(f"{name} is {getattr(hdr, name)}, expected {value}")
    if read_free_list(raw, hdr, lay) != gaps:
        raise bad("the free space table does not match the unused space")
    return gaps


class SpaceMap:
    """Free space of one CCKD file during a transaction. All header totals are
    kept as cckd_get_space / cckd_rel_space keep them; finish() then writes
    the free space and the header as cckd_flush_space + cckd_write_fsp +
    cckd_write_chdr do."""

    def __init__(self, raw, hdr: CckdHeader, lay: Layout, free: list[tuple[int, int]], l1: list[int]):
        self.raw = raw
        self.hdr = hdr
        self.lay = lay
        self.blocks = list(free)  # sorted (offset, length), never adjacent
        # cckd_read_l1: track images are kept above the L2 tables' area.
        self.l2_bounds = L1_POS + len(l1) * lay.l1_size + \
            sum(1 for off in l1 if not lay.is_null(off)) * lay.l2tab_size

    @property
    def maxsize(self) -> int:
        return max_file_size(self.lay.is64)

    def _largest(self) -> int:
        return max((ln for _, ln in self.blocks), default=0)

    def get(self, length: int, l2: bool = False) -> int:
        """cckd_get_space with CCKD_SIZE_EXACT (size == length)."""
        h = self.hdr
        if length <= NULLFMT_MAX:
            raise ValueError("no space is allocated for a null track")
        need = length + self.lay.fb_size  # a split must leave room for a FREEBLK
        if need <= h.free_largest or length == h.free_largest:
            for i, (off, ln) in enumerate(self.blocks):
                if (need <= ln or length == ln) and (l2 or off >= self.l2_bounds):
                    break
            else:
                return self._at_end(length)
            if length < ln:
                self.blocks[i] = (off + length, ln - length)
            else:
                del self.blocks[i]
                h.free_num -= 1
            if ln >= h.free_largest:
                h.free_largest = self._largest()
            h.used += length
            h.free_total -= length
            return off
        return self._at_end(length)

    def _at_end(self, length: int) -> int:
        h = self.hdr
        if h.size > self.maxsize - length:
            raise NoSpaceError("the compressed image file has reached its maximum size")
        off = h.size
        h.size += length
        h.used += length
        return off

    def release(self, off: int, length: int, size: int) -> None:
        """cckd_rel_space, merging with the neighbouring free blocks at once."""
        if length <= NULLFMT_MAX or self.lay.is_null(off):
            return
        h = self.hdr
        i = bisect.bisect_left(self.blocks, (off, 0))
        prev = self.blocks[i - 1] if i > 0 else None
        nxt = self.blocks[i] if i < len(self.blocks) else None
        if (prev and prev[0] + prev[1] > off) or (nxt and off + size > nxt[0]) or off + size > h.size:
            raise FormatError(f"released space at offset {off} overlaps free space")
        start, ln = off, size
        h.free_num += 1
        if prev and prev[0] + prev[1] == off:
            start, ln = prev[0], prev[1] + ln
            i -= 1
            del self.blocks[i]
            h.free_num -= 1
        if nxt and off + size == nxt[0]:
            ln += nxt[1]
            del self.blocks[i]
            h.free_num -= 1
        self.blocks.insert(i, (start, ln))
        h.used -= length
        h.free_total += length
        h.free_imbed -= size - length
        h.free_largest = max(h.free_largest, ln)

    def finish(self) -> None:
        """Write the free space and the header; truncate the file as needed."""
        h, lay, raw = self.hdr, self.lay, self.raw
        # cckd_flush_space: free space at the very end goes back to the OS.
        if self.blocks and sum(self.blocks[-1]) == h.size:
            _off, ln = self.blocks.pop()
            h.size -= ln
            h.free_total -= ln
        h.free_num = len(self.blocks)
        h.free_largest = self._largest()
        # cckd_write_fsp
        end = h.size
        table = None
        if not self.blocks:
            h.free_off = 0
        else:
            n = (len(self.blocks) + 1) * lay.fb_size
            fpos = next((off for off, ln in self.blocks if n <= ln), 0)
            if not fpos and self.maxsize - h.size >= n:
                fpos = end = h.size
                end += n
            if fpos:
                table = (fpos, FREE_MAGIC.ljust(lay.fb_size, b"\0") +
                         b"".join(struct.pack(lay.fb_fmt, off, ln) for off, ln in self.blocks))
                h.free_off = fpos
            else:
                h.free_off = self.blocks[0][0]
        if raw.size > end:
            raw.truncate(end)
        if table is not None:
            raw.write(*table)
        elif self.blocks:
            # Old format: link each block to the next. (Hercules' fallback
            # starts writing at offset 0 by mistake; the chain it means, and
            # the one cckd_read_fsp and cckdcdsk read, starts at free_off.)
            links = [off for off, _ in self.blocks[1:]] + [0]
            for (off, ln), nxt in zip(self.blocks, links):
                raw.write(off, struct.pack(lay.fb_fmt, nxt, ln))
        raw.write(CDH_POS, h.pack())
