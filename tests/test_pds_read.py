from datetime import date, datetime

from ckdoffline.dataset.pds import IspfStats, build_ispf, parse_ispf


def test_ispf_build_extended_when_over_64k():
    s = IspfStats(1, 0, date(2026, 1, 2), datetime(2026, 10, 25, 7, 8, 9), 70000, 65535, 4, "ME")
    b = build_ispf(s)
    assert len(b) == 40 and b[2] & 0x20
    assert parse_ispf(b) == IspfStats(1, 0, date(2026, 1, 2), datetime(2026, 10, 25, 7, 8, 9),
                                      70000, 65535, 4, "ME", flags=0x20)
    small = IspfStats(1, 0, date(2026, 1, 2), datetime(2026, 10, 25, 7, 8, 9), 12, 12, 0, "ME")
    assert len(build_ispf(small)) == 30


def test_ispf_century_digit():
    s = IspfStats(1, 0, date(1999, 12, 31), datetime(2001, 1, 1, 0, 0, 5), 1, 1, 0, "X")
    b = build_ispf(s)
    assert b[4:8].hex() == "0099365f" and b[8:12].hex() == "0101001f"
    assert parse_ispf(b) == s
