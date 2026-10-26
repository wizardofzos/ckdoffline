"""CKD track records (parse/build) and 3390 geometry and capacity.

Tracks are the uncompressed Hercules images exchanged by DasdImage:
HA (00 CC CC HH HH), R0 count + data, records R1..n, FF*8 end-of-track.
"""

import struct
from dataclasses import dataclass

from ckdoffline.errors import FormatError

HA_SIZE = 5
COUNT_SIZE = 8
ENDTRK = b"\xff" * 8


@dataclass
class Record:
    cyl: int
    head: int
    rec: int
    key: bytes
    data: bytes


class Geom3390:
    """3390 entry of dasdtab.c's CKD device table (formula 2)."""
    heads = 15
    trksize_hercules = 56832          # Hercules track image size in CKD/CCKD files
    max_track_capacity = 56664        # "r1": max data length of a single R1
    track_length = 58786              # "len": physical track length used by capacity_calc
    f1, f2, f3, f4, f5, f6 = 34, 19, 9, 6, 116, 6


GEOM_3390 = Geom3390()
EOF_RECORD = (0, 0)


def is_eof(r: Record) -> bool:
    return len(r.key) == 0 and len(r.data) == 0


def parse_track(img: bytes) -> tuple[bytes, list[Record]]:
    """Return (R0 data, [R1..n]); stops at the FF*8 end-of-track marker."""
    n = len(img)
    pos = HA_SIZE
    if n < pos + COUNT_SIZE:
        raise FormatError("track image is too short")
    r0len = img[pos + 5] + struct.unpack_from(">H", img, pos + 6)[0]
    r0 = bytes(img[pos + COUNT_SIZE:pos + COUNT_SIZE + r0len])
    pos += COUNT_SIZE + r0len
    records: list[Record] = []
    while True:
        if pos + COUNT_SIZE > n:
            raise FormatError("track image has no end-of-track marker")
        if img[pos:pos + COUNT_SIZE] == ENDTRK:
            return r0, records
        cyl, head, rec, kl, dl = struct.unpack_from(">HHBBH", img, pos)
        start = pos + COUNT_SIZE
        end = start + kl + dl
        if end > n:
            raise FormatError("record runs past the end of the track image")
        records.append(Record(cyl, head, rec, bytes(img[start:start + kl]),
                              bytes(img[start + kl:end])))
        pos = end


def build_track(cyl: int, head: int, records: list[Record],
                r0_data: bytes = b"\0" * 8) -> bytes:
    out = bytearray(b"\x00" + struct.pack(">HH", cyl, head))
    out += struct.pack(">HHBBH", cyl, head, 0, 0, len(r0_data)) + r0_data
    for r in records:
        out += struct.pack(">HHBBH", r.cyl, r.head, r.rec, len(r.key), len(r.data))
        out += r.key + r.data
    out += ENDTRK
    return bytes(out)


def _block_size(keylen: int, datalen: int) -> int:
    """Track bytes one record occupies (capacity_calc, formula 2: 3390)."""
    g = GEOM_3390
    int1 = ((datalen + g.f6) + (g.f5 * 2 - 1)) // (g.f5 * 2)
    int2 = ((keylen + g.f6) + (g.f5 * 2 - 1)) // (g.f5 * 2)
    fl1 = g.f1 * g.f2 + datalen + g.f6 + g.f4 * int1
    fl2 = 0 if keylen == 0 else g.f1 * g.f3 + keylen + g.f6 + g.f4 * int2
    fl1 = (fl1 + g.f1 - 1) // g.f1 * g.f1
    fl2 = (fl2 + g.f1 - 1) // g.f1 * g.f1
    return fl1 + fl2


def _used(records: list[tuple[int, int]]) -> int:
    return sum(_block_size(k, d) for k, d in records)


def fits_on_track(records: list[tuple[int, int]]) -> bool:
    """records are (keylen, datalen) of R1..n; HA and R0 are not counted."""
    return _used(records) <= GEOM_3390.track_length


def track_balance(records: list[tuple[int, int]]) -> int:
    """Bytes left after these records (capacity_calc's trkbaln); 0 if full."""
    return max(0, GEOM_3390.track_length - _used(records))
