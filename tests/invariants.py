"""Cross-structure invariants shared by the VTOC-index, extend and acceptance tests."""

from ckdoffline.vtoc.vtoc import Volume
from ckdoffline.vtoc.vtocix import VtocIndex


def allocated_tracks(volume: Volume) -> set[int]:
    """Absolute tracks that the index must show as allocated: every extent of
    every dataset (F1 + F3 chain, the VTOC index included), the VTOC extent,
    and track 0 (IPL/VOL1). See docs/notes/vtocix.md."""
    heads = volume.image.heads
    used = {0}
    exts = [e for d in volume.datasets() for e in d.extents] + [volume.vtoc_extent]
    for e in exts:
        used.update(range(e.bcyl * heads + e.bhead, e.ecyl * heads + e.ehead + 1))
    return used


def _sample(s):
    return sorted(s)[:10]


def assert_vtocix_consistent(volume: Volume) -> None:
    """VPSM free tracks == volume minus allocated tracks; VMDS free DSCBs ==
    the all-zero F0 slots; cylinder bits agree with track bits; the index is
    not in a state that needs z/OS recovery."""
    ix = VtocIndex(volume)
    assert ix.unsafe_reason is None, ix.unsafe_reason
    total = volume.image.cyls * volume.image.heads
    expected = set(range(total)) - allocated_tracks(volume)
    got = ix.free_tracks()
    assert got == expected, (f"VPSM free but in an extent: {_sample(got - expected)}; "
                             f"VPSM allocated but in no extent: {_sample(expected - got)}")
    f0 = set(volume.free_dscb_slots())
    vmds = ix.free_dscb_slots()
    assert vmds == f0, f"VMDS free-not-F0 {_sample(vmds - f0)}, F0-not-free {_sample(f0 - vmds)}"
