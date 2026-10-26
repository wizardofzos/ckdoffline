"""Volume: VOL1, the VTOC, DSCB access and the dataset list."""

from dataclasses import dataclass

from ckdoffline import ebcdic
from ckdoffline.errors import FormatError, NotFoundError
from ckdoffline.image.base import DasdImage
from ckdoffline.track import Record, build_track, parse_track
from ckdoffline.vtoc.dscb import (Dscb, Extent, Format0, Format1, Format3, Format4,
                                  decode_dscb, unpack_cchhr)

VOL1_CYL, VOL1_HEAD, VOL1_REC = 0, 0, 3
VOL1_LABEL = b"\xe5\xd6\xd3\xf1"            # EBCDIC "VOL1"

# DS4VTOCI bits (DS4IVTOC, DS4DIRF, DS4DOSBT), confirmed in docs/notes/vtocix.md.
# An indexed VTOC normally has both DS4VTOCI_INDEXED and DS4VTOCI_DOS on.
DS4VTOCI_INDEXED = 0x01
DS4VTOCI_DIRF = 0x04
DS4VTOCI_DOS = 0x80

DSORG0_IS, DSORG0_PS, DSORG0_DA, DSORG0_PO, DSORG0_U = 0x80, 0x40, 0x20, 0x02, 0x01
DSORG1_VSAM = 0x08
RECFM_FORMAT, RECFM_F, RECFM_V, RECFM_U = 0xC0, 0x80, 0x40, 0xC0
RECFM_TRKOFLOW, RECFM_BLOCKED, RECFM_SPANNED, RECFM_ANSI, RECFM_MACHINE = 0x20, 0x10, 0x08, 0x04, 0x02
SMS_PDSE, SMS_HFS, SMS_EXTENDED = 0x08, 0x02, 0x04
DSIND_LASTVOL = 0x80

MAX_F3_CHAIN = 64


@dataclass
class DatasetInfo:
    dsname: str
    f1_cchhr: tuple
    dsorg_str: str
    recfm_str: str
    lrecl: int
    blksize: int
    tracks_alloc: int
    tracks_used: int
    extents: list
    editable: bool
    reason: str | None


def dsorg_string(f1: Format1) -> str:
    b0, b1 = f1.dsorg >> 8, f1.dsorg & 0xFF
    if b1 & DSORG1_VSAM:
        return "VS"
    if b0 & DSORG0_IS:
        return "IS"
    if b0 & DSORG0_PS:
        return "PS"
    if b0 & DSORG0_DA:
        return "DA"
    if b0 & DSORG0_PO:
        return "PO-E" if f1.smsfg & SMS_PDSE else "PO"
    return "??"


def recfm_string(recfm: int) -> str:
    if recfm == 0:
        return ""
    s = {RECFM_F: "F", RECFM_V: "V", RECFM_U: "U"}.get(recfm & RECFM_FORMAT)
    if s is None:
        return "?"
    if recfm & RECFM_BLOCKED:
        s += "B"
    if recfm & RECFM_SPANNED:
        s += "S"
    if recfm & RECFM_ANSI:
        s += "A"
    if recfm & RECFM_MACHINE:
        s += "M"
    if recfm & RECFM_TRKOFLOW:
        s += "T"
    return s


def editability(f1: Format1, dsorg: str, recfm: str) -> tuple[bool, str | None]:
    """Global Constraints: PS and plain PO with F/FB/FBS/V/VB/U record formats."""
    if f1.smsfg & SMS_PDSE:
        return False, "PDSE is not supported"
    if f1.smsfg & SMS_HFS:
        return False, "HFS is not supported"
    if f1.smsfg & SMS_EXTENDED:
        return False, "extended-format dataset is not supported"
    if f1.dsname.startswith("SYS1.VTOCIX."):
        return False, "VTOC index is maintained by the tool"
    if dsorg not in ("PS", "PO"):
        return False, {"VS": "VSAM is not supported", "DA": "direct access is not supported",
                       "IS": "indexed sequential is not supported"}.get(dsorg, "unknown organization")
    if f1.volsq != 1 or not f1.dsind & DSIND_LASTVOL:
        return False, "multi-volume dataset"
    if f1.tracks_used > 0xFFFF:
        return False, "large format data set over 65535 tracks is not supported"
    if recfm == "":
        return False, "RECFM is not set"
    if recfm.startswith("V") and "S" in recfm:
        return False, "spanned variable records (VBS) are browse only"
    if recfm[0] not in "FVU":
        return False, "unknown record format"
    return True, None


class Volume:
    """The VTOC of an open image. Reads are always from the image (nothing is
    cached here), so they stay correct across commit and rollback."""

    def __init__(self, image: DasdImage):
        self.image = image
        label = self._vol1()
        self.volser = ebcdic.to_text(label[4:10], "1047").rstrip()
        self.vtoc_cchhr = unpack_cchhr(label[11:16])
        f4 = self.read_dscb(self.vtoc_cchhr)
        if not isinstance(f4, Format4):
            raise FormatError("VOL1 does not point at a format 4 DSCB")
        self.f4 = f4
        self.vtoc_extent = f4.vtoce
        self.dos_bit = bool(f4.vtoci & DS4VTOCI_DOS)
        self.indexed = bool(f4.vtoci & DS4VTOCI_INDEXED) and any(
            f1.dsname == f"SYS1.VTOCIX.{self.volser}" for _, f1 in self._scan_f1())

    def _vol1(self) -> bytes:
        _, recs = parse_track(self.image.read_track(VOL1_CYL, VOL1_HEAD))
        for r in recs:
            if r.rec == VOL1_REC:
                if r.key != VOL1_LABEL or len(r.data) < 16 or r.data[:4] != VOL1_LABEL:
                    break
                return r.data
        raise FormatError("no VOL1 label (not an initialised volume)")

    # -- raw DSCB access ---------------------------------------------------
    def _record(self, cchhr) -> tuple[Record, list[Record], bytes]:
        cyl, head, rec = cchhr
        r0, recs = parse_track(self.image.read_track(cyl, head))
        for r in recs:
            if r.rec == rec:
                return r, recs, r0
        raise NotFoundError(f"no DSCB at {cyl}/{head}/{rec}")

    def read_dscb(self, cchhr) -> Dscb:
        r, _, _ = self._record(cchhr)
        return decode_dscb(r.key, r.data)

    def write_dscb(self, cchhr, dscb: Dscb) -> None:
        """Rewrite one VTOC record (the whole track is rebuilt). Only valid
        inside a transaction."""
        if len(dscb.raw) != 140:
            raise FormatError("DSCB must be 140 bytes")
        cyl, head, _ = cchhr
        target, recs, r0 = self._record(cchhr)
        target.key, target.data = bytes(dscb.raw[:44]), bytes(dscb.raw[44:])
        self.image.write_track(cyl, head, build_track(cyl, head, recs, r0))

    def _scan(self):
        """Yield (cchhr, dscb) for every record in the VTOC extent."""
        e, heads = self.vtoc_extent, self.image.heads
        first, last = e.bcyl * heads + e.bhead, e.ecyl * heads + e.ehead
        for trk in range(first, last + 1):
            cyl, head = divmod(trk, heads)
            _, recs = parse_track(self.image.read_track(cyl, head))
            for r in recs:
                if len(r.key) == 44 and len(r.data) == 96:
                    yield (cyl, head, r.rec), decode_dscb(r.key, r.data)

    def _scan_f1(self):
        for cchhr, d in self._scan():
            if isinstance(d, Format1) and d.dsname:
                yield cchhr, d

    # -- lookups -----------------------------------------------------------
    def f1(self, dsname: str) -> tuple[tuple, Format1]:
        name = dsname.upper()
        for cchhr, d in self._scan_f1():
            if d.dsname == name:
                return cchhr, d
        raise NotFoundError(f"dataset {dsname} not found on {self.volser}")

    def _extents(self, f1: Format1) -> list[Extent]:
        out = list(f1.extents)
        ptr = f1.ptrds
        for _ in range(MAX_F3_CHAIN):
            if not any(ptr):
                break
            f3 = self.read_dscb(ptr)
            if not isinstance(f3, Format3):
                break
            out += f3.extents
            ptr = f3.ptrds
        else:
            raise FormatError(f"{f1.dsname}: F3 chain does not end")
        return out

    def extents_of(self, dsname: str) -> list[Extent]:
        return self._extents(self.f1(dsname)[1])

    def free_dscb_slots(self) -> list[tuple]:
        return [c for c, d in self._scan() if isinstance(d, Format0) and not any(d.raw)]

    def datasets(self) -> list[DatasetInfo]:
        out = []
        for cchhr, f1 in self._scan_f1():
            exts = self._extents(f1)
            dsorg, recfm = dsorg_string(f1), recfm_string(f1.recfm)
            ok, why = editability(f1, dsorg, recfm)
            out.append(DatasetInfo(
                f1.dsname, cchhr, dsorg, recfm, f1.lrecl, f1.blksize,
                sum(e.tracks(self.image.heads) for e in exts), f1.tracks_used,
                exts, ok, why))
        out.sort(key=lambda d: d.dsname)
        return out
