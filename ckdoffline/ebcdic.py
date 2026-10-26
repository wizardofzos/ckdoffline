"""EBCDIC code pages used for dataset text: IBM-1047 (default) and IBM-037.

Both tables are full 256-entry bijections onto U+0000..U+00FF, so every byte
decodes to a character and every character encodes back to the same byte.
"""

CODEPAGES = ("1047", "037")

# Bytes whose character differs between 1047 and 037 (1047 = 037 with these moved).
_1047_OVERRIDES = {
    0x5F: "^",       # 037: NOT SIGN
    0xB0: "¬",  # 037: CIRCUMFLEX
    0xAD: "[",       # 037: Y ACUTE
    0xBA: "Ý",  # 037: LEFT SQUARE BRACKET
    0xBB: "¨",  # 037: RIGHT SQUARE BRACKET
    0xBD: "]",       # 037: DIAERESIS
}


def _build_decode(cp):
    if cp == "037":
        return [bytes([b]).decode("cp037") for b in range(256)]
    if cp == "1047":
        table = _build_decode("037")
        for byte, char in _1047_OVERRIDES.items():
            table[byte] = char
        return table
    raise ValueError(f"unknown code page: {cp!r}")


_DECODE = {cp: _build_decode(cp) for cp in CODEPAGES}
_ENCODE = {cp: {ch: b for b, ch in enumerate(table)} for cp, table in _DECODE.items()}


def _tables(cp):
    if cp not in _DECODE:
        raise ValueError(f"unknown code page: {cp!r}")
    return _DECODE[cp], _ENCODE[cp]


def to_text(b: bytes, cp: str) -> str:
    decode, _ = _tables(cp)
    return "".join(decode[x] for x in b)


def from_text(s: str, cp: str) -> bytes:
    _, encode = _tables(cp)
    return bytes(encode[ch] for ch in s)


def is_roundtrippable(b: bytes, cp: str) -> bool:
    return from_text(to_text(b, cp), cp) == bytes(b)
