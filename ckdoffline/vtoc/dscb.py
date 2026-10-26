"""VTOC DSCBs (F0/F1/F3/F4/F5/F7), backed by their raw 140 bytes.

A DSCB on a 3390 VTOC track is a record with a 44-byte key and 96 bytes of
data. ``raw`` holds key + data, so a struct offset in dasdblks.h is also the
offset into ``raw`` (DS1FMTID = 0x2C, DS1SMSFG = 0x4E, ...). Properties read
and write ``raw`` in place; nothing is cached, so decode -> raw is lossless.
CCHHR values are (cyl, head, rec) tuples.
"""

import struct
from dataclasses import dataclass

from ckdoffline import ebcdic
from ckdoffline.errors import FormatError

KEY_LEN = 44
DATA_LEN = 96
DSCB_LEN = KEY_LEN + DATA_LEN
EXTENT_LEN = 10
FMTID_OFFSET = 44

XTTYPE_UNUSED = 0x00

CCHHR = tuple  # (cyl, head, rec)


def unpack_cchhr(b: bytes) -> tuple[int, int, int]:
    cyl, head, rec = struct.unpack(">HHB", bytes(b))
    return cyl, head, rec


def pack_cchhr(cchhr: tuple[int, int, int]) -> bytes:
    return struct.pack(">HHB", *cchhr)


@dataclass(frozen=True)
class Extent:
    type: int
    seq: int
    bcyl: int
    bhead: int
    ecyl: int
    ehead: int

    def tracks(self, heads: int) -> int:
        return (self.ecyl - self.bcyl) * heads + (self.ehead - self.bhead) + 1

    @classmethod
    def unpack(cls, b: bytes) -> "Extent":
        return cls(*struct.unpack(">BBHHHH", bytes(b)))

    def pack(self) -> bytes:
        return struct.pack(">BBHHHH", self.type, self.seq, self.bcyl, self.bhead,
                           self.ecyl, self.ehead)


def _byte(off):
    return property(lambda s: s.raw[off], lambda s, v: s.raw.__setitem__(off, v))


def _half(off):
    def get(s):
        return struct.unpack_from(">H", s.raw, off)[0]

    def put(s, v):
        struct.pack_into(">H", s.raw, off, v)
    return property(get, put)


def _cchhr(off):
    def get(s):
        return unpack_cchhr(s.raw[off:off + 5])

    def put(s, v):
        s.raw[off:off + 5] = pack_cchhr(v)
    return property(get, put)


def _ydd(off):
    """Date field: (years since 1900, day of year)."""
    def get(s):
        return s.raw[off], struct.unpack_from(">H", s.raw, off + 1)[0]

    def put(s, v):
        s.raw[off] = v[0]
        struct.pack_into(">H", s.raw, off + 1, v[1])
    return property(get, put)


def _extents(slots):
    """slots: raw offsets of consecutive extent descriptors; unused (type 0)
    descriptors are left out of the list and zeroed when written."""
    def get(s):
        out = []
        for off in slots:
            e = Extent.unpack(s.raw[off:off + EXTENT_LEN])
            if e.type != XTTYPE_UNUSED:
                out.append(e)
        return out

    def put(s, v):
        if len(v) > len(slots):
            raise ValueError(f"at most {len(slots)} extents fit in this DSCB")
        for i, off in enumerate(slots):
            s.raw[off:off + EXTENT_LEN] = v[i].pack() if i < len(v) else bytes(EXTENT_LEN)
    return property(get, put)


class Dscb:
    """Any DSCB: 44-byte key + 96 bytes data."""
    fmtid = None

    def __init__(self, raw):
        if len(raw) != DSCB_LEN:
            raise FormatError(f"DSCB is {len(raw)} bytes; expected {DSCB_LEN}")
        self.raw = bytearray(raw)

    @property
    def key(self) -> bytes:
        return bytes(self.raw[:KEY_LEN])

    @property
    def data(self) -> bytes:
        return bytes(self.raw[KEY_LEN:])

    @property
    def format_id(self) -> int:
        return self.raw[FMTID_OFFSET]


class Format0(Dscb):
    """Free DSCB slot: key and data all X'00'."""
    fmtid = 0x00


class Format1(Dscb):
    fmtid = 0xF1
    FLAG1_LARGE = 0x08   # DS1LARGE

    @property
    def dsname(self) -> str:
        return ebcdic.to_text(self.raw[:KEY_LEN], "1047").rstrip(" \x00")

    @dsname.setter
    def dsname(self, name: str) -> None:
        if len(name) > KEY_LEN:
            raise ValueError("dataset name longer than 44")
        self.raw[:KEY_LEN] = ebcdic.from_text(name.ljust(KEY_LEN), "1047")

    volser_raw = property(lambda s: bytes(s.raw[0x2D:0x33]))
    volsq = _half(0x33)
    credt = _ydd(0x35)
    expdt = _ydd(0x38)
    noepv = _byte(0x3B)
    bodbd = _byte(0x3C)
    flag1 = _byte(0x3D)
    refdt = _ydd(0x4B)
    smsfg = _byte(0x4E)
    recfm = _byte(0x54)
    optcd = _byte(0x55)
    blksize = _half(0x56)
    lrecl = _half(0x58)
    keylen = _byte(0x5A)
    rkp = _half(0x5B)
    dsind = _byte(0x5D)
    trbal = _half(0x65)
    ttthi = _byte(0x68)
    extents = _extents((0x69, 0x73, 0x7D))
    ptrds = _cchhr(0x87)

    @property
    def dsorg(self) -> int:
        """16-bit DS1DSORG (byte 0 in the high byte)."""
        return struct.unpack_from(">H", self.raw, 0x52)[0]

    @dsorg.setter
    def dsorg(self, v: int) -> None:
        struct.pack_into(">H", self.raw, 0x52, v)

    @property
    def lstar(self) -> tuple[int, int]:
        """(TT, R) of DS1LSTAR."""
        return struct.unpack_from(">HB", self.raw, 0x62)

    @lstar.setter
    def lstar(self, v: tuple[int, int]) -> None:
        struct.pack_into(">HB", self.raw, 0x62, *v)

    @property
    def scalo(self) -> tuple[int, int]:
        """(units byte, 24-bit quantity) of DS1SCALO."""
        return self.raw[0x5E], int.from_bytes(self.raw[0x5F:0x62], "big")

    @scalo.setter
    def scalo(self, v: tuple[int, int]) -> None:
        self.raw[0x5E] = v[0]
        self.raw[0x5F:0x62] = v[1].to_bytes(3, "big")

    @property
    def tracks_used(self) -> int:
        """Tracks up to and including the one DS1LSTAR points at; 0 if
        DS1LSTAR is zero. LARGE format adds DS1TTTHI as the high byte."""
        if not any(self.raw[0x62:0x65]):
            return 0
        tt = self.lstar[0]
        if self.flag1 & self.FLAG1_LARGE:
            tt |= self.ttthi << 16
        return tt + 1


def _keyid():
    """The 4-byte key identifier (DS3KEYID X'03030303', DS5KEYID X'05050505')."""
    return property(lambda s: bytes(s.raw[:4]))


def _blank(cls, n: int):
    raw = bytearray(DSCB_LEN)
    raw[:4] = bytes([n]) * 4
    raw[FMTID_OFFSET] = cls.fmtid
    return cls(raw)


class Format3(Dscb):
    """Additional extents: 4 in the key area, 9 after the format id
    (DS3EXTNT at 4, DS3FMTID at 44, DS3ADEXT at 45, DS3PTRDS at 135)."""
    fmtid = 0xF3
    KEYID = 0x03
    MAX_EXTENTS = 13
    keyid = _keyid()
    extents = _extents(tuple(4 + EXTENT_LEN * i for i in range(4))
                       + tuple(45 + EXTENT_LEN * i for i in range(9)))
    ptrds = _cchhr(135)

    @classmethod
    def blank(cls) -> "Format3":
        """A new F3: key X'03030303', format id X'F3', everything else zero."""
        return _blank(cls, cls.KEYID)


class Format4(Dscb):
    fmtid = 0xF4
    hpchr = _cchhr(45)
    dsrec = _half(50)
    vtoci = _byte(58)
    noext = _byte(59)
    devtk = _half(66)
    devdt = _byte(74)
    devdb = _byte(75)
    f6ptr = _cchhr(100)

    @property
    def devsz(self) -> tuple[int, int]:
        """DS4DEVSZ: (cylinders, tracks per cylinder) of the volume."""
        return struct.unpack_from(">HH", self.raw, 62)

    @property
    def vtoce(self) -> Extent:
        return Extent.unpack(self.raw[105:105 + EXTENT_LEN])

    @vtoce.setter
    def vtoce(self, e: Extent) -> None:
        self.raw[105:105 + EXTENT_LEN] = e.pack()


@dataclass(frozen=True)
class F5Extent:
    """DS5AVEXT entry: relative track of the first free track, then the
    length as whole cylinders plus additional tracks."""
    btrk: int
    ncyl: int
    ntrk: int

    def tracks(self, heads: int) -> int:
        return self.ncyl * heads + self.ntrk

    @classmethod
    def unpack(cls, b: bytes) -> "F5Extent":
        return cls(*struct.unpack(">HHB", bytes(b)))

    def pack(self) -> bytes:
        return struct.pack(">HHB", self.btrk, self.ncyl, self.ntrk)


F5_EXTENT_LEN = 5
# DS5AVEXT + DS5EXTAV (8 in the key, offset 4), DS5MAVET (18 after DS5FMTID, offset 45)
_F5_SLOTS = (tuple(4 + F5_EXTENT_LEN * i for i in range(8))
             + tuple(45 + F5_EXTENT_LEN * i for i in range(18)))


class Format5(Dscb):
    """Free-space map for a non-indexed VTOC (IBM Advanced Services Table 5)."""
    fmtid = 0xF5
    KEYID = 0x05
    MAX_EXTENTS = len(_F5_SLOTS)
    keyid = _keyid()
    ptrds = _cchhr(135)

    @classmethod
    def blank(cls) -> "Format5":
        """A new F5: key X'05050505', format id X'F5', no free extents."""
        return _blank(cls, cls.KEYID)

    @property
    def extents(self) -> list[F5Extent]:
        """The used entries (an all-zero entry is unused)."""
        out = []
        for off in _F5_SLOTS:
            b = self.raw[off:off + F5_EXTENT_LEN]
            if any(b):
                out.append(F5Extent.unpack(b))
        return out

    @extents.setter
    def extents(self, v: list[F5Extent]) -> None:
        if len(v) > len(_F5_SLOTS):
            raise ValueError(f"at most {len(_F5_SLOTS)} free extents fit in a format 5 DSCB")
        for i, off in enumerate(_F5_SLOTS):
            self.raw[off:off + F5_EXTENT_LEN] = v[i].pack() if i < len(v) else bytes(F5_EXTENT_LEN)


class Format7(Dscb):
    fmtid = 0xF7
    ptrds = _cchhr(135)


_BY_ID = {c.fmtid: c for c in (Format0, Format1, Format3, Format4, Format5, Format7)}


def decode_dscb(key: bytes, data: bytes) -> Dscb:
    """Dispatch on the format id. Unknown ids (F2, F6, F8, F9...) come back as
    a plain Dscb so the raw bytes still round-trip."""
    if len(key) != KEY_LEN or len(data) != DATA_LEN:
        raise FormatError(f"DSCB record has key {len(key)} / data {len(data)}; "
                          f"expected {KEY_LEN} / {DATA_LEN}")
    raw = bytes(key) + bytes(data)
    return _BY_ID.get(raw[FMTID_OFFSET], Dscb)(raw)
