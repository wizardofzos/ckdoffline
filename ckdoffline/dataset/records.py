"""Deblock / reblock of FB, FBS, VB, VBS and U blocks."""

from ckdoffline.errors import FormatError, UnsupportedError, ValidationError

SEG_COMPLETE, SEG_FIRST, SEG_LAST, SEG_MIDDLE = 0, 1, 2, 3


def _kind(recfm: str) -> str:
    return recfm[:1].upper()


def _is_vbs(recfm: str) -> bool:
    return _kind(recfm) == "V" and "S" in recfm.upper()[1:]


def _descriptors(block: bytes, what: str):
    """Yield (flags, payload) for each RDW/SDW in a V block (after the BDW)."""
    if len(block) < 4:
        raise FormatError(f"{what}: block shorter than a BDW")
    bdw = int.from_bytes(block[:2], "big")
    if bdw < 4 or bdw > len(block):
        raise FormatError(f"{what}: bad BDW length {bdw} for a {len(block)}-byte block")
    pos = 4
    while pos < bdw:
        if pos + 4 > bdw:
            raise FormatError(f"{what}: truncated RDW at offset {pos}")
        n = int.from_bytes(block[pos:pos + 2], "big")
        if n < 4 or pos + n > bdw:
            raise FormatError(f"{what}: bad RDW length {n} at offset {pos}")
        yield block[pos + 2], block[pos + 4:pos + n]
        pos += n


def deblock(block: bytes, recfm: str, lrecl: int) -> list[bytes]:
    """Records of one block (V records are returned without their RDW).
    VBS needs the whole block sequence: use deblock_stream."""
    k = _kind(recfm)
    if k == "U":
        return [bytes(block)]
    if k == "F":
        if lrecl <= 0:
            raise FormatError("fixed-format dataset has no LRECL")
        return [bytes(block[i:i + lrecl]) for i in range(0, len(block), lrecl)]
    if k == "V":
        if _is_vbs(recfm):
            raise UnsupportedError("VBS blocks must be read with deblock_stream")
        return [bytes(p) for _, p in _descriptors(block, "variable block")]
    raise UnsupportedError(f"unsupported RECFM {recfm!r}")


def deblock_stream(blocks, recfm: str, lrecl: int) -> list[bytes]:
    """Records of a whole dataset; reassembles VBS segments across blocks."""
    out: list[bytes] = []
    if not _is_vbs(recfm):
        for b in blocks:
            out += deblock(b, recfm, lrecl)
        return out
    partial: bytearray | None = None
    for b in blocks:
        for flags, payload in _descriptors(b, "VBS block"):
            seg = flags & 0x03
            if seg == SEG_COMPLETE:
                if partial is not None:
                    raise FormatError("VBS: complete segment inside a spanned record")
                out.append(bytes(payload))
            elif seg == SEG_FIRST:
                if partial is not None:
                    raise FormatError("VBS: first segment inside a spanned record")
                partial = bytearray(payload)
            else:
                if partial is None:
                    raise FormatError("VBS: continuation segment without a first segment")
                partial += payload
                if seg == SEG_LAST:
                    out.append(bytes(partial))
                    partial = None
    if partial is not None:
        raise FormatError("VBS: dataset ends inside a spanned record")
    return out


def _fail(line_no: int, msg: str) -> ValidationError:
    e = ValidationError(f"line {line_no}: {msg}")
    e.line_no = line_no
    return e


def reblock(records: list[bytes], recfm: str, lrecl: int, blksize: int) -> list[bytes]:
    """Pack records into blocks. FB records must be exactly lrecl, V records at
    most lrecl-4 bytes."""
    k = _kind(recfm)
    blocked = "B" in recfm.upper()[1:]
    if k == "U":
        return [bytes(r) for r in records]
    if k == "F":
        for i, r in enumerate(records, 1):
            if len(r) != lrecl:
                raise _fail(i, f"record is {len(r)} bytes, LRECL is {lrecl}")
        per = max(1, blksize // lrecl) if blocked else 1
        return [b"".join(records[i:i + per]) for i in range(0, len(records), per)]
    if k == "V":
        if _is_vbs(recfm):
            raise UnsupportedError("VBS datasets are read-only")
        blocks: list[bytes] = []
        cur: list[bytes] = []
        size = 4
        for i, r in enumerate(records, 1):
            if len(r) > lrecl - 4:
                raise _fail(i, f"record is {len(r)} bytes, maximum is {lrecl - 4}")
            n = len(r) + 4
            if 4 + n > blksize:
                raise _fail(i, f"record of {len(r)} bytes does not fit BLKSIZE {blksize}")
            if cur and (not blocked or size + n > blksize):
                blocks.append(_pack_v(cur, size))
                cur, size = [], 4
            cur.append(r)
            size += n
        if cur:
            blocks.append(_pack_v(cur, size))
        return blocks
    raise UnsupportedError(f"unsupported RECFM {recfm!r}")


def _pack_v(recs: list[bytes], size: int) -> bytes:
    out = bytearray(size.to_bytes(2, "big") + b"\0\0")
    for r in recs:
        out += (len(r) + 4).to_bytes(2, "big") + b"\0\0" + r
    return bytes(out)
