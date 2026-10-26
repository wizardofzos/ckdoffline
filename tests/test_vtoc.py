import re

import pytest

from ckdoffline.errors import CkdError
from ckdoffline.image.base import open_image
from ckdoffline.vtoc.dscb import Format1
from ckdoffline.vtoc.vtoc import Volume

from oracle import _run, ORACLE_BIN
from pathlib import Path

_ROW = re.compile(r"^\s*(\d+)\s+([0-9A-F]{4})/([0-9A-F]{4})\s+([0-9A-F]{4})/([0-9A-F]{4})\s+(\S+)")
_DSN_WIDTH = 44


def dasdls_info(image):
    """Parse `dasdls -hdr -info -cchh` into {dsname: {org, recfm, lrecl, blksz,
    trks, extents}}. Rows with only an extent and a name are continuations.
    dasdls prints no DS1LSTAR-based track count or editability, so tracks_used
    and editable are not compared against it."""
    out = _run([ORACLE_BIN / "dasdls", "-hdr", "-info", "-cchh", Path(image).resolve()]).stdout.decode()
    res = {}
    for line in out.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        ext = tuple(int(m.group(i), 16) for i in (2, 3, 4, 5))
        name = m.group(6)
        tail = line[line.index(name) + _DSN_WIDTH:]
        if not tail.strip():
            res[name]["extents"].append(ext)
            continue
        rest = tail[22:].split()
        res[name] = {"org": tail[7:9].strip(), "recfm": tail[11:16].strip(),
                     "lrecl": int(tail[17:22].strip() or 0), "blksz": int(rest[0]),
                     "trks": int(rest[2]), "extents": [ext]}
    return res


def _check_against_dasdls(image):
    vol = Volume(open_image(image, False))
    ls = dasdls_info(image)
    dss = vol.datasets()
    assert sorted(ls) == [d.dsname for d in dss]
    for d in dss:
        row = ls[d.dsname]
        assert d.dsorg_str.split("-")[0] == row["org"] or (row["org"] == "" and d.dsorg_str == "??"), d.dsname
        # dasdls shows an unset RECFM (X'00') as "U"
        assert d.recfm_str == row["recfm"] or (row["recfm"] == "U" and d.recfm_str == ""), d.dsname
        assert (d.lrecl, d.blksize, d.tracks_alloc) == (row["lrecl"], row["blksz"], row["trks"]), d.dsname
        assert [(e.bcyl, e.bhead, e.ecyl, e.ehead) for e in d.extents] == row["extents"], d.dsname
    return vol, dss


@pytest.mark.parametrize("key", ["ckd", "cckd_zlib", "cckd_bz2", "cckd64"])
def test_fixture_dslist(fixture_images, key):
    vol, dss = _check_against_dasdls(fixture_images[key])
    assert vol.volser == "TEST01"
    by = {d.dsname: d for d in dss}
    assert set(by) == {"TEST.FB80.PDS", "TEST.VB.PDS", "TEST.LOAD", "TEST.FB.SEQ", "TEST.VB.SEQ"}
    assert (by["TEST.FB80.PDS"].dsorg_str, by["TEST.FB80.PDS"].recfm_str) == ("PO", "FB")
    assert (by["TEST.VB.PDS"].lrecl, by["TEST.VB.PDS"].recfm_str) == (255, "VB")
    assert by["TEST.LOAD"].recfm_str == "U"
    assert by["TEST.FB.SEQ"].dsorg_str == "PS" and by["TEST.FB.SEQ"].editable
    # empty dataset: dasdload leaves DS1LSTAR = TTR 0/0/1 (EOF only), so the
    # TT+1 rule gives 1; tracks_used == 0 only when DS1LSTAR is all zero
    assert vol.f1("TEST.VB.SEQ")[1].lstar == (0, 1)
    assert by["TEST.VB.SEQ"].tracks_used == 1
    assert all(d.editable for d in dss)


def test_tracks_used_large():
    f1 = Format1(bytes(140))
    assert f1.tracks_used == 0
    f1.lstar = (9, 1)
    assert f1.tracks_used == 10
    f1.ttthi, f1.flag1 = 2, 0
    assert f1.tracks_used == 10            # not LARGE: DS1TTTHI ignored
    f1.flag1 = 0x08
    assert f1.tracks_used == (2 << 16) + 10


def test_write_dscb(fixture_images):
    path = fixture_images["ckd"]
    img = open_image(path, True)
    vol = Volume(img)
    cchhr, f1 = vol.f1("TEST.FB.SEQ")
    old = bytes(f1.raw)
    f1.dsind |= 0x02
    with pytest.raises(CkdError):                       # outside a transaction
        vol.write_dscb(cchhr, f1)
    img.begin()
    vol.write_dscb(cchhr, f1)
    img.rollback()
    assert bytes(Volume(img).f1("TEST.FB.SEQ")[1].raw) == old
    img.begin()
    vol.write_dscb(cchhr, f1)
    img.commit()
    img.close()
    vol2 = Volume(open_image(path, False))
    c2, g = vol2.f1("TEST.FB.SEQ")
    assert c2 == cchhr and bytes(g.raw) == old[:0x5D] + bytes([old[0x5D] | 0x02]) + old[0x5E:]
    assert [d.dsname for d in vol2.datasets()] == sorted(d.dsname for d in vol2.datasets())
    assert len(vol2.datasets()) == 5


@pytest.mark.parametrize("volsq,dsind", [(2, 0x80), (1, 0x00)])
def test_multi_volume_not_editable(fixture_images, volsq, dsind):
    img = open_image(fixture_images["ckd"], True)
    vol = Volume(img)
    c, f1 = vol.f1("TEST.FB.SEQ")
    f1.raw[0x33:0x35] = volsq.to_bytes(2, "big")
    f1.dsind = dsind
    img.begin()
    vol.write_dscb(c, f1)
    img.commit()
    d = {x.dsname: x for x in vol.datasets()}["TEST.FB.SEQ"]
    assert not d.editable and d.reason == "multi-volume dataset"
