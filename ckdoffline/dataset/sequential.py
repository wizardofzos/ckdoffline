"""Sequential (PS) dataset read and write."""

from ckdoffline.dataset.extents import DatasetIO
from ckdoffline.dataset.records import deblock_stream, reblock
from ckdoffline.errors import UnsupportedError, ValidationError
from ckdoffline.vtoc.vtoc import Volume, dsorg_string, editability, recfm_string


def read_sequential(volume: Volume, dsname: str) -> list[bytes]:
    io = DatasetIO(volume, dsname)
    recfm = recfm_string(io.f1.recfm)
    return deblock_stream((r.data for _, r in io.blocks()), recfm, io.f1.lrecl)


def write_sequential(volume: Volume, dsname: str, records: list[bytes]) -> None:
    """Replace the dataset's records, reblocked with its RECFM/LRECL/BLKSIZE.
    Must run inside a transaction. Validation (ValidationError naming the
    line, UnsupportedError, and the NoSpaceError pre-check for extents and
    free space) happens before anything is staged. Any error after that
    (e.g. an extend that still fails) poisons the transaction: commit()
    refuses and the caller must roll back."""
    io = DatasetIO(volume, dsname)
    f1 = io.f1
    dsorg, recfm = dsorg_string(f1), recfm_string(f1.recfm)
    ok, why = editability(f1, dsorg, recfm)
    if not ok:
        raise UnsupportedError(f"{f1.dsname} cannot be edited: {why}")
    if dsorg != "PS":
        raise UnsupportedError(f"{f1.dsname} is not a sequential dataset")
    if f1.keylen:
        raise UnsupportedError(f"{f1.dsname}: keyed datasets are not supported")
    blocks = reblock(records, recfm, f1.lrecl, f1.blksize)
    io.write_blocks((0, 1), blocks, ending="PS")


def patch_sequential_blocks(volume: Volume, dsname: str, patches: dict[int, bytes]) -> None:
    """Hex overtype of a sequential RECFM U dataset: {block index (0-based): new bytes}, same
    length only, rewritten in place (keys, R0 and every other record kept; nothing moves), then
    DS1DSIND/DS1REFDT are updated. Every patch is checked before anything is staged, so the
    caller's transaction applies all of them or none. Messages number blocks from 1."""
    io = DatasetIO(volume, dsname)
    f1 = io.f1
    dsorg, recfm = dsorg_string(f1), recfm_string(f1.recfm)
    ok, why = editability(f1, dsorg, recfm)
    if not ok:
        raise UnsupportedError(f"{f1.dsname} cannot be edited: {why}")
    if dsorg != "PS":
        raise UnsupportedError(f"{f1.dsname} is not a sequential dataset")
    if not recfm.startswith("U"):
        raise UnsupportedError(f"{f1.dsname} is RECFM {recfm}: only RECFM U data sets are "
                               f"patched in hex; edit it as text")
    if not patches:
        raise ValidationError("no blocks to patch")
    blocks = list(io.blocks())
    by_track: dict[int, dict[int, tuple[None, bytes]]] = {}
    for index, new in sorted(patches.items()):
        if not 0 <= index < len(blocks):
            raise ValidationError(f"{f1.dsname} has {len(blocks)} blocks; "
                                  f"there is no block {index + 1}")
        (tt, r), rec = blocks[index]
        if len(new) != len(rec.data):
            raise ValidationError(f"block {index + 1} is {len(rec.data)} bytes; the patch is "
                                  f"{len(new)} (block lengths cannot change)")
        by_track.setdefault(tt, {})[r] = (None, bytes(new))
    with volume.image.mutating():
        for tt, recs in by_track.items():
            io.rewrite_records(tt, recs)
        io.mark_updated()
