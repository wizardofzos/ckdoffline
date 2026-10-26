import pytest

from ckdoffline import ebcdic


def test_known_chars():
    assert ebcdic.from_text("HELLO", "1047") == bytes.fromhex("C8C5D3D3D6")
    # 1047 vs 037 differ on six bytes
    assert ebcdic.from_text("[", "1047") == b"\xad"
    assert ebcdic.from_text("]", "1047") == b"\xbd"
    assert ebcdic.from_text("^", "1047") == b"\x5f"
    assert ebcdic.from_text("¬", "1047") == b"\xb0"
    assert ebcdic.from_text("Ý", "1047") == b"\xba"
    assert ebcdic.from_text("¨", "1047") == b"\xbb"

    assert ebcdic.from_text("[", "037") == b"\xba"
    assert ebcdic.from_text("]", "037") == b"\xbb"
    assert ebcdic.from_text("¬", "037") == b"\x5f"
    assert ebcdic.from_text("^", "037") == b"\xb0"
    assert ebcdic.from_text("Ý", "037") == b"\xad"
    assert ebcdic.from_text("¨", "037") == b"\xbd"

    # official CCSID mapping, not the z/OS UNIX LF swap
    for cp in ("1047", "037"):
        assert ebcdic.to_text(b"\x15", cp) == "\u0085"
        assert ebcdic.to_text(b"\x25", cp) == "\u000a"


@pytest.mark.parametrize("cp", ["1047", "037"])
def test_bijection(cp):
    allbytes = bytes(range(256))
    assert ebcdic.from_text(ebcdic.to_text(allbytes, cp), cp) == allbytes


@pytest.mark.parametrize("cp", ["1047", "037"])
def test_to_text_covers_latin1_range(cp):
    text = ebcdic.to_text(bytes(range(256)), cp)
    assert sorted(ord(c) for c in text) == list(range(256))


def test_codepages_constant():
    assert ebcdic.CODEPAGES == ("1047", "037")


def test_is_roundtrippable():
    assert ebcdic.is_roundtrippable(bytes.fromhex("C8C5D3D3D6"), "1047")
    assert ebcdic.is_roundtrippable(bytes(range(256)), "037")


def test_unknown_codepage_raises():
    with pytest.raises(ValueError):
        ebcdic.to_text(b"A", "500")
    with pytest.raises(ValueError):
        ebcdic.from_text("A", "500")
    with pytest.raises(ValueError):
        ebcdic.is_roundtrippable(b"A", "500")
