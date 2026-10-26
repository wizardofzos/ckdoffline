"""Free space, secondary allocation, and adding an extent to a dataset.

Free space is computed from the extents: track 0 (IPL/VOL1), the VTOC extent
and every dataset extent (F1 + F3 chain, SYS1.VTOCIX included) are allocated,
everything else up to DS4DEVSZ is free (docs/notes/vtocix.md). That set must
equal the volume's own free-space map before anything is changed: the VPSM
on an indexed VTOC, the F5 chain on a non-indexed one. A disagreement is
refused, never repaired.

extend() writes inside the caller's transaction. Every check that can fail
(limits, space, DSCB slots, index state) runs before the first write; if a
write fails after that, the exception propagates and the caller rolls back.

Field definitions: IBM z/OS DFSMSdfp Advanced Services, Tables 2 (F1), 3
(F3), 4 (F4) and 5 (F5); Hyperion dasdblks.h for the byte offsets.
"""

import math

from ckdoffline.errors import FormatError, NoSpaceError, UnsupportedError
from ckdoffline.track import fits_on_track
from ckdoffline.vtoc.dscb import Extent, F5Extent, Format0, Format1, Format3, Format5, DSCB_LEN
from ckdoffline.vtoc.vtoc import (DS4VTOCI_DIRF, DS4VTOCI_DOS, DS4VTOCI_INDEXED, MAX_F3_CHAIN,
                                  Volume, dsorg_string, editability, recfm_string)
from ckdoffline.vtoc.vtocix import VtocIndex

# DS4VTOCI. IBM defines X'04' as DS4DIRF and X'40' as DS4DVTOC (index
# disabled). Hyperion's dasdblks.h still calls X'40' DS4VTOCI_DIRF: that is
# the outdated meaning, so DS4VTOCI_DIRF = X'04' in vtoc.py is correct and
# must not be "fixed". Either bit set means z/OS has recovery to do: refuse.
DS4VTOCI_OLD_DIRF = 0x40
DS4VTOCI_EFVLD = 0x20       # free space is in format-7 DSCBs (not supported)

# DS1SCAL1 (DS1SCALO byte 0)
SCAL1_UNITS = 0xC0
UNITS_ABSTR, UNITS_BLK, UNITS_TRK, UNITS_CYL = 0x00, 0x40, 0x80, 0xC0
SCAL1_EXT = 0x10            # DS1EXT: DS1SCEXT is valid
SCAL1_ROUND = 0x01
# DS1SCEXT
SCEXT_OFF = 0x4F
SCXTF_AVB = 0x80            # DS1SCAVB: DS1SCXTV is the requested block length

XTTYPE_DATA = 0x01
XTTYPE_CYLBOUND = 0x81
XTTYPE_USERLBL = 0x40

MAX_EXTENTS = 16            # non-VSAM, non-extended datasets, per volume
MAX_TRACKS_NOT_LARGE = 0xFFFF   # TT of DS1LSTAR / TTR addressing
F5_MAX_TRACKS = 0xFFFF      # DS5AVEXT relative tracks are 2 bytes
F5_RECORD = 2               # the first F5 is the second DSCB of the VTOC


# -- helpers -------------------------------------------------------------------
def _ranges(tracks) -> list[tuple[int, int]]:
    """Sorted tracks -> coalesced [start, end) ranges."""
    out: list[list[int]] = []
    for t in sorted(tracks):
        if out and out[-1][1] == t:
            out[-1][1] = t + 1
        else:
            out.append([t, t + 1])
    return [(s, e) for s, e in out]


def _volume_tracks(volume: Volume) -> int:
    cyls, heads = volume.f4.devsz
    if heads != volume.image.heads or not 0 < cyls <= volume.image.cyls:
        raise FormatError(f"DS4DEVSZ {cyls}/{heads} does not match the image "
                          f"({volume.image.cyls} cylinders, {volume.image.heads} heads)")
    return cyls * heads


def _allocated(volume: Volume, total: int) -> set[int]:
    """Track 0, the VTOC and every dataset extent; FormatError if two of
    them overlap or one lies outside the volume."""
    heads = volume.image.heads
    used = {0}
    owners = [(d.dsname, e) for d in volume.datasets() for e in d.extents]
    owners.append(("the VTOC", volume.vtoc_extent))
    for name, e in owners:
        lo, hi = e.bcyl * heads + e.bhead, e.ecyl * heads + e.ehead
        if e.bhead >= heads or e.ehead >= heads or not 0 <= lo <= hi < total:
            raise FormatError(f"{name}: extent {e.bcyl}/{e.bhead}-{e.ecyl}/{e.ehead} "
                              "is outside the volume")
        span = range(lo, hi + 1)
        if not used.isdisjoint(span):
            raise FormatError(f"{name}: extent {e.bcyl}/{e.bhead}-{e.ecyl}/{e.ehead} "
                              "overlaps other allocated space")
        used.update(span)
    return used


def _f5_chain(volume: Volume) -> list[tuple[tuple, Format5]]:
    e = volume.vtoc_extent
    ptr = (e.bcyl, e.bhead, F5_RECORD)
    out = []
    for _ in range(e.tracks(volume.image.heads) * volume.f4.devdt):
        d = volume.read_dscb(ptr)
        if not isinstance(d, Format5):
            raise FormatError(f"no format 5 DSCB at {'/'.join(map(str, ptr))}")
        out.append((ptr, d))
        ptr = d.ptrds
        if not any(ptr):
            return out
    raise FormatError("format 5 DSCB chain does not end")


def _f5_free(chain, heads: int) -> set[int]:
    free: set[int] = set()
    for _, f5 in chain:
        for x in f5.extents:
            span = range(x.btrk, x.btrk + x.tracks(heads))
            if not free.isdisjoint(span):
                raise FormatError("free space map (format 5) out of sync: overlapping free extents")
            free.update(span)
    return free


def _map_kind(volume: Volume, f4) -> str:
    """'index', 'f5', or 'none' (no valid map: DOS bit on, or format 7)."""
    if volume.indexed:
        return "index"
    if f4.vtoci & (DS4VTOCI_INDEXED | DS4VTOCI_DOS | DS4VTOCI_EFVLD):
        return "none"
    return "f5"


def _free_tracks(volume: Volume, kind: str, ix: VtocIndex | None) -> set[int]:
    total = _volume_tracks(volume)
    free = set(range(total)) - _allocated(volume, total)
    if kind == "index":
        if ix.free_tracks() != free:
            raise FormatError("VTOC index out of sync: the VPSM disagrees with the extents")
    elif kind == "f5":
        if _f5_free(_f5_chain(volume), volume.image.heads) != free:
            raise FormatError("free space map (format 5) out of sync with the extents")
    return free


def free_extents(volume: Volume) -> list[tuple[int, int]]:
    """Free absolute track ranges [start, end), lowest first. Cross-checked
    against the VPSM (indexed) or the F5 chain (non-indexed, DOS bit off)."""
    f4 = volume.read_dscb(volume.vtoc_cchhr)
    kind = _map_kind(volume, f4)
    return _ranges(_free_tracks(volume, kind, VtocIndex(volume) if kind == "index" else None))


# -- secondary quantity -----------------------------------------------------------
def _blocks_per_track(keylen: int, blklen: int) -> int:
    n = 0
    while fits_on_track([(keylen, blklen)] * (n + 1)):
        n += 1
    if n == 0:
        raise FormatError(f"block size {blklen} does not fit on a track")
    return n


def secondary_tracks(f1: Format1, heads: int) -> tuple[int, bool]:
    """(tracks, cylinder_aligned) of one secondary allocation per DS1SCALO.
    BLK units: the block length is DS1SCXTV when DS1EXT + DS1SCAVB say it is
    the requested block length, else DS1BLKL; tracks = ceil(blocks / blocks
    that fit on a track). CYL units or ROUND: whole cylinders, aligned."""
    scal1, qty = f1.scalo
    units = scal1 & SCAL1_UNITS
    if units == UNITS_ABSTR:
        raise UnsupportedError(f"{f1.dsname}: absolute track (ABSTR) allocation cannot be extended")
    if qty == 0:
        raise NoSpaceError(f"{f1.dsname}: no secondary allocation")
    if units == UNITS_TRK:
        tracks = qty
    elif units == UNITS_CYL:
        tracks = qty * heads
    else:
        if scal1 & SCAL1_EXT:
            if f1.raw[SCEXT_OFF] != SCXTF_AVB:
                raise UnsupportedError(f"{f1.dsname}: secondary space in average record units "
                                       "(AVGREC) is not supported")
            blklen = int.from_bytes(f1.raw[SCEXT_OFF + 1:SCEXT_OFF + 3], "big")
        else:
            blklen = f1.blksize
        if blklen == 0:
            raise UnsupportedError(f"{f1.dsname}: block size 0; cannot size a block secondary")
        tracks = math.ceil(qty / _blocks_per_track(f1.keylen, blklen))
    aligned = units == UNITS_CYL or bool(scal1 & SCAL1_ROUND)
    if aligned:
        tracks = math.ceil(tracks / heads) * heads
    return tracks, aligned


# -- DSCB chains ---------------------------------------------------------------------
def _f3_chain(volume: Volume, f1: Format1) -> list[tuple[tuple, Format3]]:
    out, ptr = [], f1.ptrds
    for _ in range(MAX_F3_CHAIN):
        if not any(ptr):
            return out
        d = volume.read_dscb(ptr)
        if not isinstance(d, Format3):
            raise UnsupportedError(f"{f1.dsname}: extent chain holds a format "
                                   f"{d.format_id:02X} DSCB")
        out.append((ptr, d))
        ptr = d.ptrds
    raise FormatError(f"{f1.dsname}: F3 chain does not end")


def _f5_count(ranges) -> int:
    return max(1, math.ceil(len(ranges) / Format5.MAX_EXTENTS))


def _write_f5_chain(volume: Volume, ranges: list[tuple[int, int]]) -> None:
    """Rewrite the F5 chain to hold exactly these free ranges, in address
    order. The first F5 stays where it is; extra F5s take the lowest free
    DSCB slots, unneeded ones become F0 again; DS4DSREC follows. Only for a
    non-indexed VTOC, inside a transaction."""
    heads = volume.image.heads
    entries = [F5Extent(s, *divmod(e - s, heads)) for s, e in ranges]
    if any(x.btrk > F5_MAX_TRACKS for x in entries):
        raise UnsupportedError("format 5 free space beyond track 65535")
    chain = _f5_chain(volume)
    need = _f5_count(ranges)
    slots = sorted(volume.free_dscb_slots())
    if need - len(chain) > len(slots):
        raise NoSpaceError("VTOC full: no free DSCB for a format 5 DSCB")
    f4 = volume.read_dscb(volume.vtoc_cchhr)
    dsrec = f4.dsrec
    while len(chain) < need:
        chain.append((slots.pop(0), Format5.blank()))
        dsrec = max(0, dsrec - 1)
    chain, freed = chain[:need], chain[need:]
    per = Format5.MAX_EXTENTS
    for i, (c, f5) in enumerate(chain):
        f5.extents = entries[i * per:(i + 1) * per]
        f5.ptrds = chain[i + 1][0] if i + 1 < need else (0, 0, 0)
        volume.write_dscb(c, f5)
    for c, _ in freed:
        volume.write_dscb(c, Format0(bytes(DSCB_LEN)))
        dsrec += 1
    if dsrec != f4.dsrec:
        f4.dsrec = dsrec
        volume.write_dscb(volume.vtoc_cchhr, f4)


# -- extend ------------------------------------------------------------------------
def _first_fit(free: list[tuple[int, int]], need: int, align: int) -> int | None:
    for s, e in free:
        start = math.ceil(s / align) * align
        if start + need <= e:
            return start
    return None


def extend(volume: Volume, dsname: str, min_tracks: int) -> Extent:
    """Add one extent of at least max(min_tracks, secondary) tracks to the
    dataset, first fit at the lowest address, and update F1/F3, F4, and the
    VTOC index or the F5 chain. Must run inside a transaction."""
    heads = volume.image.heads
    f4 = volume.read_dscb(volume.vtoc_cchhr)
    if f4.vtoci & (DS4VTOCI_DIRF | DS4VTOCI_OLD_DIRF):
        raise UnsupportedError("the VTOC is marked as needing recovery (DIRF); "
                               "let z/OS repair it first")
    kind = _map_kind(volume, f4)
    ix = None
    if kind == "index":
        ix = VtocIndex(volume)
        if ix.unsafe_reason:
            raise UnsupportedError(f"VTOC index cannot be updated: {ix.unsafe_reason}")
    elif f4.vtoci & DS4VTOCI_INDEXED:
        raise UnsupportedError(f"the VTOC index bit is on but SYS1.VTOCIX.{volume.volser} is missing")
    elif f4.vtoci & DS4VTOCI_DOS:
        raise UnsupportedError("free space map invalid (DOS bit)")
    elif f4.vtoci & DS4VTOCI_EFVLD:
        raise UnsupportedError("format 7 free space DSCBs are not supported")
    elif _volume_tracks(volume) > F5_MAX_TRACKS:
        raise UnsupportedError("a non-indexed VTOC on a volume over 65535 tracks is not supported")

    f1c, f1 = volume.f1(dsname)
    ok, why = editability(f1, dsorg_string(f1), recfm_string(f1.recfm))
    if not ok:
        raise UnsupportedError(f"{f1.dsname} cannot be extended: {why}")
    chain = _f3_chain(volume, f1)
    extents = f1.extents + [e for _, f3 in chain for e in f3.extents]
    if any(e.type == XTTYPE_USERLBL for e in extents):
        raise UnsupportedError(f"{f1.dsname}: user label extents are not supported")
    if f1.noepv != len(extents):
        raise FormatError(f"{f1.dsname}: DS1NOEPV is {f1.noepv} but {len(extents)} extents are recorded")
    if len(extents) >= MAX_EXTENTS:
        raise NoSpaceError(f"{f1.dsname} already has {MAX_EXTENTS} extents")

    sec, aligned = secondary_tracks(f1, heads)
    need = max(min_tracks, sec)
    if aligned:
        need = math.ceil(need / heads) * heads
    have = sum(e.tracks(heads) for e in extents)
    if not f1.flag1 & Format1.FLAG1_LARGE and have + need > MAX_TRACKS_NOT_LARGE:
        raise NoSpaceError(f"{f1.dsname} would exceed {MAX_TRACKS_NOT_LARGE} tracks")

    free = _free_tracks(volume, kind, ix)
    start = _first_fit(_ranges(free), need, heads if aligned else 1)
    if start is None:
        raise NoSpaceError(f"volume full: no free extent of {need} tracks on {volume.volser}")
    new_tracks = range(start, start + need)
    if not free.issuperset(new_tracks):          # cannot happen; never overlap an extent
        raise FormatError("internal error: new extent overlaps allocated space")
    (bcyl, bhead), (ecyl, ehead) = divmod(start, heads), divmod(start + need - 1, heads)
    new = Extent(XTTYPE_CYLBOUND if aligned else XTTYPE_DATA, len(extents), bcyl, bhead, ecyl, ehead)

    # DSCB slots: an F3 when F1 and the last F3 are full; more F5s if needed
    need_f3 = len(extents) >= 3 and (not chain or len(chain[-1][1].extents) >= Format3.MAX_EXTENTS)
    slots = sorted(volume.free_dscb_slots())
    if kind == "index" and ix.free_dscb_slots() != set(slots):
        raise FormatError("VTOC index out of sync: the VMDS disagrees with the free DSCBs")
    new_free = _ranges(free.difference(new_tracks))
    extra_f5 = max(0, _f5_count(new_free) - len(_f5_chain(volume))) if kind == "f5" else 0
    if need_f3 + extra_f5 > len(slots):
        raise NoSpaceError(f"VTOC full: no free DSCB on {volume.volser}")

    # stage the index (memory only; refuses if a bit is already set)
    f3_slot = slots[0] if need_f3 else None
    if ix is not None:
        ix.mark_tracks(new_tracks, used=True)
        if need_f3:
            ix.mark_dscb(f3_slot, used=True)

    # writes
    if len(extents) < 3:
        f1.extents = f1.extents + [new]
    elif need_f3:
        f3 = Format3.blank()
        f3.extents = [new]
        volume.write_dscb(f3_slot, f3)
        if chain:
            last_c, last = chain[-1]
            last.ptrds = f3_slot
            volume.write_dscb(last_c, last)
        else:
            f1.ptrds = f3_slot
    else:
        last_c, last = chain[-1]
        last.extents = last.extents + [new]
        volume.write_dscb(last_c, last)
    f1.noepv += 1
    volume.write_dscb(f1c, f1)
    if ix is not None:
        ix.write_staged()       # DS4DSREC is not maintained on an indexed VTOC
    else:
        if need_f3:
            f4.dsrec = max(0, f4.dsrec - 1)
            volume.write_dscb(volume.vtoc_cchhr, f4)
        _write_f5_chain(volume, new_free)
    return new
