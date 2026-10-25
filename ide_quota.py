"""Decrypt local IDE login files without third-party crypto packages.

Trae stores user info with its own AES wrapper. Qoder CN IDE stores it
with Chromium safeStorage (v10, PBKDF2, AES-128-CBC). The screen container
is Linux and does not have the cryptography package, so AES lives here.
"""

from __future__ import annotations

import hashlib

# Trae byteCrypto AES mask. These are the app's fixed tables, not a user secret.
_TRAE_MASK_A = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
    8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37,
])
_TRAE_MASK_B = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125,
])

_SBOX = bytes([
    99, 124, 119, 123, 242, 107, 111, 197, 48, 1, 103, 43, 254, 215, 171, 118,
    202, 130, 201, 125, 250, 89, 71, 240, 173, 212, 162, 175, 156, 164, 114, 192,
    183, 253, 147, 38, 54, 63, 247, 204, 52, 165, 229, 241, 113, 216, 49, 21,
    4, 199, 35, 195, 24, 150, 5, 154, 7, 18, 128, 226, 235, 39, 178, 117,
    9, 131, 44, 26, 27, 110, 90, 160, 82, 59, 214, 179, 41, 227, 47, 132,
    83, 209, 0, 237, 32, 252, 177, 91, 106, 203, 190, 57, 74, 76, 88, 207,
    208, 239, 170, 251, 67, 77, 51, 133, 69, 249, 2, 127, 80, 60, 159, 168,
    81, 163, 64, 143, 146, 157, 56, 245, 188, 182, 218, 33, 16, 255, 243, 210,
    205, 12, 19, 236, 95, 151, 68, 23, 196, 167, 126, 61, 100, 93, 25, 115,
    96, 129, 79, 220, 34, 42, 144, 136, 70, 238, 184, 20, 222, 94, 11, 219,
    224, 50, 58, 10, 73, 6, 36, 92, 194, 211, 172, 98, 145, 149, 228, 121,
    231, 200, 55, 109, 141, 213, 78, 169, 108, 86, 244, 234, 101, 122, 174, 8,
    186, 120, 37, 46, 28, 166, 180, 198, 232, 221, 116, 31, 75, 189, 139, 138,
    112, 62, 181, 102, 72, 3, 246, 14, 97, 53, 87, 185, 134, 193, 29, 158,
    225, 248, 152, 17, 105, 217, 142, 148, 155, 30, 135, 233, 206, 85, 40, 223,
    140, 161, 137, 13, 191, 230, 66, 104, 65, 153, 45, 15, 176, 84, 187, 22,
])
def _inv_sbox() -> bytes:
    table = [0] * 256
    for index, value in enumerate(_SBOX):
        table[value] = index
    return bytes(table)


_INV = _inv_sbox()
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(value: int) -> int:
    return ((value << 1) & 0xFF) ^ (0x1B if value & 0x80 else 0)


def _mul(value: int, factor: int) -> int:
    result = 0
    while factor:
        if factor & 1:
            result ^= value
        value = _xtime(value)
        factor >>= 1
    return result


def _expand_key(key: bytes) -> list[bytes]:
    words = [list(key[index:index + 4]) for index in range(0, 16, 4)]
    for index in range(4, 44):
        word = words[index - 1][:]
        if index % 4 == 0:
            word = word[1:] + word[:1]
            word = [_SBOX[byte] for byte in word]
            word[0] ^= _RCON[index // 4]
        words.append([left ^ right for left, right in zip(words[index - 4], word)])
    return [bytes(sum(words[index:index + 4], [])) for index in range(0, 44, 4)]


def _decrypt_block(block: bytes, rounds: list[bytes]) -> bytes:
    def _inv_shift(cells: list[int]) -> list[int]:
        return [cells[index] for index in (0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3)]

    def _inv_mix(cells: list[int]) -> list[int]:
        mixed = []
        for column in range(4):
            block = cells[column * 4:(column + 1) * 4]
            mixed.extend([
                _mul(block[0], 14) ^ _mul(block[1], 11) ^ _mul(block[2], 13) ^ _mul(block[3], 9),
                _mul(block[0], 9) ^ _mul(block[1], 14) ^ _mul(block[2], 11) ^ _mul(block[3], 13),
                _mul(block[0], 13) ^ _mul(block[1], 9) ^ _mul(block[2], 14) ^ _mul(block[3], 11),
                _mul(block[0], 11) ^ _mul(block[1], 13) ^ _mul(block[2], 9) ^ _mul(block[3], 14),
            ])
        return mixed

    state = [block[index] ^ rounds[-1][index] for index in range(16)]
    for round_key in reversed(rounds[1:-1]):
        state = _inv_shift(state)
        state = [_INV[byte] for byte in state]
        state = [state[index] ^ round_key[index] for index in range(16)]
        state = _inv_mix(state)
    state = _inv_shift(state)
    state = [_INV[byte] for byte in state]
    return bytes(state[index] ^ rounds[0][index] for index in range(16))


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Decrypt AES-128-CBC. PKCS#7 padding is removed when it is valid."""
    if len(key) != 16 or len(iv) != 16 or len(data) % 16:
        raise ValueError("bad aes input")
    rounds = _expand_key(key)
    previous = iv
    plain = bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        decrypted = _decrypt_block(block, rounds)
        plain.extend(left ^ right for left, right in zip(decrypted, previous))
        previous = block
    pad = plain[-1]
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        del plain[-pad:]
    return bytes(plain)


def chrome_safe_decrypt(blob: bytes, password: str) -> bytes:
    """Decrypt a Chromium/Electron v10 safeStorage blob."""
    if not blob.startswith(b"v10"):
        raise ValueError("not a v10 safeStorage blob")
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, dklen=16)
    return aes128_cbc_decrypt(key, b" " * 16, blob[3:])


def trae_userinfo_decrypt(blob: bytes) -> bytes:
    """Decrypt Trae's iCubeAuthInfo blob back to JSON bytes."""
    if blob[:2] != b"tc":
        raise ValueError("not a trae user blob")
    key_material = blob[6:38]
    mixed = hashlib.sha512(key_material).digest() + bytes(
        _TRAE_MASK_A[index] ^ _TRAE_MASK_B[index] for index in range(64)
    )
    mixed = hashlib.sha512(mixed).digest() + mixed[64:]
    plain = aes128_cbc_decrypt(mixed[:16], mixed[16:32], blob[38:])
    digest, body = plain[:64], plain[64:]
    if hashlib.sha512(body).digest() != digest:
        raise ValueError("trae user blob failed the checksum")
    return body
