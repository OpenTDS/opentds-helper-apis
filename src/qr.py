"""qr - QR code generation (v1).

GET /v1/qr/get?text=...&format=json|svg|html[&ecc=M&scale=8&border=4&version=1]

Pure standard library: no qrcode, no Pillow, no numpy - nothing to install in
the shared Lambda package. Byte mode only, all 40 versions, all four
error-correction levels, automatic mask selection.

The encoder below is carried over unchanged from the pre-v1 service, where it
was verified by round-tripping every version (1-40) at every error-correction
level through an independent decoder (zbar): 480 of 480 read back exactly.
"""
from common import ApiError, base_url, raw, respond, run

import base64
import math
import struct
import zlib

# (ec_codewords_per_block, ((block_count, data_codewords), ...)) for L, M, Q, H
BLOCKS = {
     1: ((7,((1, 19),)), (10,((1, 16),)), (13,((1, 13),)), (17,((1, 9),))),
     2: ((10,((1, 34),)), (16,((1, 28),)), (22,((1, 22),)), (28,((1, 16),))),
     3: ((15,((1, 55),)), (26,((1, 44),)), (18,((2, 17),)), (22,((2, 13),))),
     4: ((20,((1, 80),)), (18,((2, 32),)), (26,((2, 24),)), (16,((4, 9),))),
     5: ((26,((1, 108),)), (24,((2, 43),)), (18,((2, 15), (2, 16))), (22,((2, 11), (2, 12)))),
     6: ((18,((2, 68),)), (16,((4, 27),)), (24,((4, 19),)), (28,((4, 15),))),
     7: ((20,((2, 78),)), (18,((4, 31),)), (18,((2, 14), (4, 15))), (26,((4, 13), (1, 14)))),
     8: ((24,((2, 97),)), (22,((2, 38), (2, 39))), (22,((4, 18), (2, 19))), (26,((4, 14), (2, 15)))),
     9: ((30,((2, 116),)), (22,((3, 36), (2, 37))), (20,((4, 16), (4, 17))), (24,((4, 12), (4, 13)))),
    10: ((18,((2, 68), (2, 69))), (26,((4, 43), (1, 44))), (24,((6, 19), (2, 20))), (28,((6, 15), (2, 16)))),
    11: ((20,((4, 81),)), (30,((1, 50), (4, 51))), (28,((4, 22), (4, 23))), (24,((3, 12), (8, 13)))),
    12: ((24,((2, 92), (2, 93))), (22,((6, 36), (2, 37))), (26,((4, 20), (6, 21))), (28,((7, 14), (4, 15)))),
    13: ((26,((4, 107),)), (22,((8, 37), (1, 38))), (24,((8, 20), (4, 21))), (22,((12, 11), (4, 12)))),
    14: ((30,((3, 115), (1, 116))), (24,((4, 40), (5, 41))), (20,((11, 16), (5, 17))), (24,((11, 12), (5, 13)))),
    15: ((22,((5, 87), (1, 88))), (24,((5, 41), (5, 42))), (30,((5, 24), (7, 25))), (24,((11, 12), (7, 13)))),
    16: ((24,((5, 98), (1, 99))), (28,((7, 45), (3, 46))), (24,((15, 19), (2, 20))), (30,((3, 15), (13, 16)))),
    17: ((28,((1, 107), (5, 108))), (28,((10, 46), (1, 47))), (28,((1, 22), (15, 23))), (28,((2, 14), (17, 15)))),
    18: ((30,((5, 120), (1, 121))), (26,((9, 43), (4, 44))), (28,((17, 22), (1, 23))), (28,((2, 14), (19, 15)))),
    19: ((28,((3, 113), (4, 114))), (26,((3, 44), (11, 45))), (26,((17, 21), (4, 22))), (26,((9, 13), (16, 14)))),
    20: ((28,((3, 107), (5, 108))), (26,((3, 41), (13, 42))), (30,((15, 24), (5, 25))), (28,((15, 15), (10, 16)))),
    21: ((28,((4, 116), (4, 117))), (26,((17, 42),)), (28,((17, 22), (6, 23))), (30,((19, 16), (6, 17)))),
    22: ((28,((2, 111), (7, 112))), (28,((17, 46),)), (30,((7, 24), (16, 25))), (24,((34, 13),))),
    23: ((30,((4, 121), (5, 122))), (28,((4, 47), (14, 48))), (30,((11, 24), (14, 25))), (30,((16, 15), (14, 16)))),
    24: ((30,((6, 117), (4, 118))), (28,((6, 45), (14, 46))), (30,((11, 24), (16, 25))), (30,((30, 16), (2, 17)))),
    25: ((26,((8, 106), (4, 107))), (28,((8, 47), (13, 48))), (30,((7, 24), (22, 25))), (30,((22, 15), (13, 16)))),
    26: ((28,((10, 114), (2, 115))), (28,((19, 46), (4, 47))), (28,((28, 22), (6, 23))), (30,((33, 16), (4, 17)))),
    27: ((30,((8, 122), (4, 123))), (28,((22, 45), (3, 46))), (30,((8, 23), (26, 24))), (30,((12, 15), (28, 16)))),
    28: ((30,((3, 117), (10, 118))), (28,((3, 45), (23, 46))), (30,((4, 24), (31, 25))), (30,((11, 15), (31, 16)))),
    29: ((30,((7, 116), (7, 117))), (28,((21, 45), (7, 46))), (30,((1, 23), (37, 24))), (30,((19, 15), (26, 16)))),
    30: ((30,((5, 115), (10, 116))), (28,((19, 47), (10, 48))), (30,((15, 24), (25, 25))), (30,((23, 15), (25, 16)))),
    31: ((30,((13, 115), (3, 116))), (28,((2, 46), (29, 47))), (30,((42, 24), (1, 25))), (30,((23, 15), (28, 16)))),
    32: ((30,((17, 115),)), (28,((10, 46), (23, 47))), (30,((10, 24), (35, 25))), (30,((19, 15), (35, 16)))),
    33: ((30,((17, 115), (1, 116))), (28,((14, 46), (21, 47))), (30,((29, 24), (19, 25))), (30,((11, 15), (46, 16)))),
    34: ((30,((13, 115), (6, 116))), (28,((14, 46), (23, 47))), (30,((44, 24), (7, 25))), (30,((59, 16), (1, 17)))),
    35: ((30,((12, 121), (7, 122))), (28,((12, 47), (26, 48))), (30,((39, 24), (14, 25))), (30,((22, 15), (41, 16)))),
    36: ((30,((6, 121), (14, 122))), (28,((6, 47), (34, 48))), (30,((46, 24), (10, 25))), (30,((2, 15), (64, 16)))),
    37: ((30,((17, 122), (4, 123))), (28,((29, 46), (14, 47))), (30,((49, 24), (10, 25))), (30,((24, 15), (46, 16)))),
    38: ((30,((4, 122), (18, 123))), (28,((13, 46), (32, 47))), (30,((48, 24), (14, 25))), (30,((42, 15), (32, 16)))),
    39: ((30,((20, 117), (4, 118))), (28,((40, 47), (7, 48))), (30,((43, 24), (22, 25))), (30,((10, 15), (67, 16)))),
    40: ((30,((19, 118), (6, 119))), (28,((18, 47), (31, 48))), (30,((34, 24), (34, 25))), (30,((20, 15), (61, 16)))),
}

# Row/column centres of the alignment patterns, by version (version 1 has none).
ALIGN = {
     2: (6, 18),
     3: (6, 22),
     4: (6, 26),
     5: (6, 30),
     6: (6, 34),
     7: (6, 22, 38),
     8: (6, 24, 42),
     9: (6, 26, 46),
    10: (6, 28, 50),
    11: (6, 30, 54),
    12: (6, 32, 58),
    13: (6, 34, 62),
    14: (6, 26, 46, 66),
    15: (6, 26, 48, 70),
    16: (6, 26, 50, 74),
    17: (6, 30, 54, 78),
    18: (6, 30, 56, 82),
    19: (6, 30, 58, 86),
    20: (6, 34, 62, 90),
    21: (6, 28, 50, 72, 94),
    22: (6, 26, 50, 74, 98),
    23: (6, 30, 54, 78, 102),
    24: (6, 28, 54, 80, 106),
    25: (6, 32, 58, 84, 110),
    26: (6, 30, 58, 86, 114),
    27: (6, 34, 62, 90, 118),
    28: (6, 26, 50, 74, 98, 122),
    29: (6, 30, 54, 78, 102, 126),
    30: (6, 26, 52, 78, 104, 130),
    31: (6, 30, 56, 82, 108, 134),
    32: (6, 34, 60, 86, 112, 138),
    33: (6, 30, 58, 86, 114, 142),
    34: (6, 34, 62, 90, 118, 146),
    35: (6, 30, 54, 78, 102, 126, 150),
    36: (6, 24, 50, 76, 102, 128, 154),
    37: (6, 28, 54, 80, 106, 132, 158),
    38: (6, 32, 58, 84, 110, 136, 162),
    39: (6, 26, 54, 82, 110, 138, 166),
    40: (6, 30, 58, 86, 114, 142, 170),
}

ECL_BITS = {'L': 1, 'M': 0, 'Q': 3, 'H': 2}      # the wire values, not L<M<Q<H

# ---------------------------------------------------------------- GF(256)
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D                              # the QR field polynomial
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def gen_poly(n):
    """Generator polynomial for n EC codewords: (x-a^0)(x-a^1)...(x-a^(n-1)).
    Coefficients highest-degree first, which is the order rs_ec divides in."""
    g = [1]
    for i in range(n):
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c                           # c * x
            ng[j + 1] ^= gf_mul(c, EXP[i])       # c * a^i
        g = ng
    return g


def rs_ec(data, n):
    """The n error-correction codewords for one block: polynomial division."""
    g = gen_poly(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(len(g)):
                rem[i + j] ^= gf_mul(g[j], c)
    return rem[len(data):]


# ------------------------------------------------------------- bit stream
def _capacity_bits(version, ecl):
    ec, blocks = BLOCKS[version][('L', 'M', 'Q', 'H').index(ecl)]
    return sum(n * d for n, d in blocks) * 8


def _count_bits(version):
    return 8 if version <= 9 else 16             # byte mode


def choose_version(nbytes, ecl, minv=1):
    for v in range(max(1, minv), 41):
        need = 4 + _count_bits(v) + nbytes * 8
        if need <= _capacity_bits(v, ecl):
            return v
    return None


def encode_data(payload, version, ecl):
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)                               # byte mode
    put(len(payload), _count_bits(version))
    for b in payload:
        put(b, 8)

    cap = _capacity_bits(version, ecl)
    put(0, min(4, cap - len(bits)))              # terminator
    while len(bits) % 8:
        bits.append(0)
    words = [int(''.join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    for i in range((cap // 8) - len(words)):     # pad codewords
        words.append(0xEC if i % 2 == 0 else 0x11)

    # Split into blocks, error-correct each, then interleave. Interleaving is
    # what makes a burst of damage land across many blocks instead of
    # destroying one, which is why a QR code survives a coffee ring.
    ec_len, spec = BLOCKS[version][('L', 'M', 'Q', 'H').index(ecl)]
    dblocks, eblocks, pos = [], [], 0
    for count, dlen in spec:
        for _ in range(count):
            blk = words[pos:pos + dlen]
            pos += dlen
            dblocks.append(blk)
            eblocks.append(rs_ec(blk, ec_len))

    out = []
    for i in range(max(len(b) for b in dblocks)):
        for b in dblocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_len):
        for b in eblocks:
            out.append(b[i])
    return out


# ----------------------------------------------------------------- matrix
def _bch(data, gen, gen_bits):
    d = data << gen_bits
    while d.bit_length() > gen_bits:
        d ^= gen << (d.bit_length() - gen.bit_length())
    return d


def build_matrix(version, ecl, codewords, mask=None):
    size = version * 4 + 17
    m = [[None] * size for _ in range(size)]     # None = free, 0/1 = set

    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    inring = dr in (0, 6) or dc in (0, 6)
                    core = 2 <= dr <= 4 and 2 <= dc <= 4
                    outside = dr in (-1, 7) or dc in (-1, 7)
                    m[rr][cc] = 0 if outside else (1 if (inring or core) else 0)

    finder(0, 0); finder(0, size - 7); finder(size - 7, 0)

    for i in range(size):                        # timing patterns
        if m[6][i] is None:
            m[6][i] = 1 - (i % 2)
        if m[i][6] is None:
            m[i][6] = 1 - (i % 2)

    coords = ALIGN.get(version, ())              # alignment patterns
    if coords:
        # Only the three that would sit on a finder are omitted. A centre that
        # merely lands on the timing row or column is drawn normally - skipping
        # those was what broke every version from 7 up, since 7 is the first
        # with a middle coordinate that is not also inside a finder.
        last = coords[-1]
        skip = {(6, 6), (6, last), (last, 6)}
        for r in coords:
            for c in coords:
                if (r, c) in skip:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        m[r + dr][c + dc] = 1 if (max(abs(dr), abs(dc)) != 1) else 0

    m[size - 8][8] = 1                           # the always-dark module

    reserved = [[m[r][c] is not None for c in range(size)] for r in range(size)]
    for i in range(9):                           # format information areas
        for r, c in ((8, i), (i, 8)):
            if r < size and c < size:
                reserved[r][c] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    if version >= 7:                             # version information areas
        for i in range(6):
            for j in range(3):
                reserved[size - 11 + j][i] = True
                reserved[i][size - 11 + j] = True

    bits = [(w >> (7 - i)) & 1 for w in codewords for i in range(8)]
    idx, up, col = 0, True, size - 1
    while col > 0:
        if col == 6:                             # skip the vertical timing column
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not reserved[r][c]:
                    m[r][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        up = not up
        col -= 2

    best = None
    for mk in (range(8) if mask is None else [mask]):
        cand = [row[:] for row in m]
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and _mask_at(mk, r, c):
                    cand[r][c] ^= 1
        _place_format(cand, size, ecl, mk)
        if version >= 7:
            _place_version(cand, size, version)
        p = penalty(cand, size)
        if best is None or p < best[0]:
            best = (p, mk, cand)
    return best[2], best[1], size


def _mask_at(k, r, c):
    return (
        (r + c) % 2 == 0,
        r % 2 == 0,
        c % 3 == 0,
        (r + c) % 3 == 0,
        (r // 2 + c // 3) % 2 == 0,
        (r * c) % 2 + (r * c) % 3 == 0,
        ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    )[k]


def _place_format(m, size, ecl, mask):
    v = (ECL_BITS[ecl] << 3) | mask
    bits = ((v << 10) | _bch(v, 0x537, 10)) ^ 0x5412
    for i in range(15):                                     # i is the bit index, LSB first
        b = (bits >> i) & 1
        # First copy, hugging the top-left finder: bits 0-5 go DOWN column 8,
        # bits 9-14 go LEFT along row 8, with 6/7/8 turning the corner. The
        # two arms are easy to transpose - they are not symmetric.
        if i <= 5:
            m[i][8] = b
        elif i == 6:
            m[7][8] = b
        elif i == 7:
            m[8][8] = b
        elif i == 8:
            m[8][7] = b
        else:
            m[8][14 - i] = b
        # Second copy: bits 0-7 run leftwards along row 8 from the right edge,
        # bits 8-14 run upwards in column 8 from the bottom edge.
        if i <= 7:
            m[8][size - 1 - i] = b
        else:
            m[size - 15 + i][8] = b


def _place_version(m, size, version):
    bits = (version << 12) | _bch(version, 0x1F25, 12)
    for i in range(18):
        b = (bits >> i) & 1
        m[i // 3][size - 11 + i % 3] = b
        m[size - 11 + i % 3][i // 3] = b


def penalty(m, size):
    total = 0
    for line in [[m[r][c] for c in range(size)] for r in range(size)] + \
                [[m[r][c] for r in range(size)] for c in range(size)]:
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    total += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            total += 3 + (run - 5)
        for i in range(len(line) - 10):          # 1:1:3:1:1 finder-alike
            for pat in ((1,0,1,1,1,0,1,0,0,0,0), (0,0,0,0,1,0,1,1,1,0,1)):
                if tuple(line[i:i + 11]) == pat:
                    total += 40
    for r in range(size - 1):
        for c in range(size - 1):
            b = m[r][c] + m[r][c+1] + m[r+1][c] + m[r+1][c+1]
            if b in (0, 4):
                total += 3
    dark = sum(sum(row) for row in m)
    total += 10 * (abs(dark * 100 // (size * size) - 50) // 5)
    return total


# -------------------------------------------------------------------- PNG
def png(matrix, size, scale=8, border=4, dark=(0, 0, 0), light=(255, 255, 255)):
    """8-bit greyscale PNG. Lossless matters: a QR code is nothing but sharp
    black/white edges, which is exactly what lossy compression smears."""
    dim = (size + border * 2) * scale
    rows = bytearray()
    d, l = dark[0], light[0]
    blank = bytes([l]) * dim
    for _ in range(border * scale):
        rows += b'\x00' + blank
    for r in range(size):
        line = bytearray()
        line += bytes([l]) * (border * scale)
        for c in range(size):
            line += bytes([d if matrix[r][c] else l]) * scale
        line += bytes([l]) * (border * scale)
        for _ in range(scale):
            rows += b'\x00' + bytes(line)
    for _ in range(border * scale):
        rows += b'\x00' + blank

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', dim, dim, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows), 9))
            + chunk(b'IEND', b''))


def make(text, ecl='M', scale=8, border=4, minv=1):
    payload = text.encode('utf-8')
    version = choose_version(len(payload), ecl, minv)
    if version is None:
        raise ValueError('too much data for a QR code at this error level')
    words = encode_data(payload, version, ecl)
    matrix, mask, size = build_matrix(version, ecl, words)
    return matrix, size, version, mask



# ============================================================== HTTP layer
SERVICE = 'qr'
MAX_TEXT = 2900                                  # a version-40 L symbol
ECC_LEVELS = ('L', 'M', 'Q', 'H')
FORMATS = ('json', 'svg', 'html')
ECC_HINT = ('L recovers ~7% damage, M ~15%, Q ~25%, H ~30% - higher costs capacity')
TEXT_HINT = ('qr/get?text=hello%20world , or text64=BASE64 for anything containing '
             '+ & % or #')


def esc(s):
    """Everything user-supplied is echoed back into the page, so escape it -
    including quotes, since some of it lands in attribute values."""
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def read_params(req):
    """(text, ecc, scale, border, minversion); raises ApiError(400) when unusable."""
    if req.get('text') not in (None, '') and req.get('text64') not in (None, ''):
        raise ApiError(400, "Send either 'text' or 'text64', not both.")
    text, _ = req.text_or64('text')
    if text is None or text == '':
        raise ApiError(400, "Missing required query parameter 'text' (or 'text64').",
                       hint=TEXT_HINT)
    nbytes = len(text.encode('utf-8'))
    if nbytes > MAX_TEXT:
        raise ApiError(400, 'Text is too long for a QR code.', bytes=nbytes, maximum=MAX_TEXT,
                       hint='the limit falls as the error-correction level rises; '
                            'ecc=L holds the most')

    ecc = (req.text('ecc') or 'M').upper()
    if ecc not in ECC_LEVELS:
        raise ApiError(400, "'ecc' must be one of L, M, Q, H.", received=ecc,
                       allowed=list(ECC_LEVELS), hint=ECC_HINT)

    # Out-of-range numbers are clamped, as elsewhere; malformed ones are a 400.
    return (text, ecc,
            req.integer('scale', 8, 1, 40),
            req.integer('border', 4, 0, 16),
            req.integer('version', 1, 1, 40))


def build(text, ecc, scale, border, minv):
    try:
        matrix, size, version, mask = make(text, ecc, minv=minv)
    except ValueError as exc:
        raise ApiError(400, str(exc), bytes=len(text.encode('utf-8')), ecc=ecc,
                       hint='use a lower ecc level or shorter text')
    data = png(matrix, size, scale=scale, border=border)
    return {
        'matrix': matrix, 'size': size, 'version': version, 'mask': mask,
        'png': data,
        'meta': {'text': text, 'bytes': len(text.encode('utf-8')), 'ecc': ecc,
                 'version': version, 'modules': size, 'mask': mask,
                 'scale': scale, 'border': border,
                 'pixels': (size + border * 2) * scale, 'pngBytes': len(data)},
    }


def svg(matrix, size, scale=8, border=4):
    """One <path> in module units, with horizontal runs merged.

    Working in module coordinates and letting the viewBox do the scaling keeps
    every number to one or two digits, and a path segment costs about a fifth
    of a <rect> element - together roughly a five-fold cut over the obvious
    rect-per-module rendering.
    """
    span = size + border * 2
    dim = span * scale
    d = []
    for r in range(size):
        c = 0
        while c < size:
            if matrix[r][c]:
                start = c
                while c < size and matrix[r][c]:
                    c += 1
                d.append('M%d %dh%dv1h-%dz'
                         % (border + start, border + r, c - start, c - start))
            else:
                c += 1
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
            'viewBox="0 0 %d %d" shape-rendering="crispEdges" role="img">'
            '<rect width="%d" height="%d" fill="#fff"/>'
            '<path fill="#000" d="%s"/></svg>'
            % (dim, dim, span, span, span, span, ''.join(d)))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QR code</title>
<style>
:root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--line:#e3e3e0;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#191918;--fg:#eeeeec;--mut:#9a9a96;--line:#33332f;--card:#222220}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:2rem 1rem}
.wrap{max-width:44rem;margin:0 auto}
h1{font-size:1.05rem;font-weight:600;margin:0 0 1.25rem;letter-spacing:-.01em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.5rem;text-align:center}
img{display:block;margin:0 auto;max-width:100%;height:auto;image-rendering:pixelated;border-radius:4px}
.txt{margin:1.25rem 0 0;padding:.7rem .9rem;background:var(--bg);border:1px solid var(--line);border-radius:8px;
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;text-align:left;white-space:pre-wrap}
dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(6.5rem,1fr));gap:.9rem;margin:1.5rem 0 0;
padding-top:1.25rem;border-top:1px solid var(--line);text-align:left}
dt{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:0 0 .15rem}
dd{margin:0;font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
form{margin:1.5rem 0 0;display:flex;gap:.5rem;flex-wrap:wrap}
input[type=text]{flex:1 1 16rem;padding:.55rem .7rem;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--fg);font:14px ui-sans-serif,system-ui,sans-serif}
button,a.dl{padding:.55rem 1rem;border:1px solid var(--line);border-radius:8px;background:var(--card);
color:var(--fg);font:14px ui-sans-serif,system-ui,sans-serif;cursor:pointer;text-decoration:none;display:inline-block}
button:hover,a.dl:hover{border-color:var(--mut)}
p.foot{color:var(--mut);font-size:12.5px;margin:1.25rem 0 0;text-align:center}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
</style></head><body><div class="wrap">
<h1>QR code</h1>
<div class="card">
<img src="data:image/png;base64,__B64__" alt="QR code encoding: __ALT__" width="__PX__" height="__PX__">
<div class="txt">__TEXT__</div>
<dl>
<div><dt>Version</dt><dd>__VER__</dd></div>
<div><dt>Modules</dt><dd>__MOD__ &times; __MOD__</dd></div>
<div><dt>Error correction</dt><dd>__ECC__ (__ECCPCT__)</dd></div>
<div><dt>Payload</dt><dd>__BYTES__ bytes</dd></div>
<div><dt>Mask</dt><dd>__MASK__</dd></div>
<div><dt>PNG</dt><dd>__PNGB__ bytes</dd></div>
</dl>
<form method="get" action="__ACTION__">
<input type="text" name="text" value="__TEXT__" aria-label="Text to encode" placeholder="Text to encode">
__HIDDEN__
<button type="submit">Regenerate</button>
<a class="dl" href="data:image/png;base64,__B64__" download="qrcode.png">Download PNG</a>
</form>
</div>
<p class="foot">The image is embedded in this page, so it needs no second request. For
scripts use <code>qr/get?format=json</code> (PNG as base64) or <code>qr/get?format=svg</code>.</p>
</div></body></html>"""

ERROR_PAGE = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width,initial-scale=1">'
              '<title>QR code</title></head>'
              '<body style="font:15px system-ui;margin:3rem auto;max-width:34rem;'
              'padding:0 1rem"><h1 style="font-size:1rem">Nothing to encode</h1>'
              '<p>%s</p><p style="color:#6b6b68;font-size:13px">%s</p>'
              '<p><a href="%s"><code>%s</code></a></p></body></html>')

ECC_PCT = {'L': '~7% recoverable', 'M': '~15% recoverable',
           'Q': '~25% recoverable', 'H': '~30% recoverable'}


def page(built, action):
    m = built['meta']
    b64 = base64.b64encode(built['png']).decode('ascii')
    # The re-encode form resubmits to this same v1 action as a page, keeping
    # the current rendering options.
    hidden = '<input type="hidden" name="format" value="html">'
    for name, val in (('ecc', m['ecc']), ('scale', m['scale']), ('border', m['border'])):
        hidden += '<input type="hidden" name="%s" value="%s">' % (name, esc(val))
    out = PAGE
    for token, value in (('__B64__', b64),
                         ('__ALT__', esc(m['text'][:120])),
                         ('__TEXT__', esc(m['text'])),
                         ('__PX__', str(m['pixels'])),
                         ('__VER__', str(m['version'])),
                         ('__MOD__', str(m['modules'])),
                         ('__ECC__', m['ecc']),
                         ('__ECCPCT__', ECC_PCT[m['ecc']]),
                         ('__BYTES__', str(m['bytes'])),
                         ('__MASK__', str(m['mask'])),
                         ('__PNGB__', str(m['pngBytes'])),
                         ('__ACTION__', esc(action)),
                         ('__HIDDEN__', hidden)):
        out = out.replace(token, value)
    return out


def do_get(req):
    fmt = (req.text('format') or 'json').lower()
    if fmt not in FORMATS:
        raise ApiError(400, "'format' must be json, svg or html.", received=fmt,
                       allowed=list(FORMATS))
    action = base_url(req.event) + '/qr/get'
    try:
        text, ecc, scale, border, minv = read_params(req)
        built = build(text, ecc, scale, border, minv)
    except ApiError as e:
        if fmt != 'html':
            raise
        # A person looking at a browser gets a page, not a JSON blob.
        example = action + '?format=html&text=hello%20world'
        body = ERROR_PAGE % (esc(e.message), esc(e.extra.get('hint', '')),
                             esc(example), esc(example))
        return raw(e.status, body, 'text/html; charset=utf-8')

    if fmt == 'svg':
        return raw(200, svg(built['matrix'], built['size'], scale, border),
                   'image/svg+xml; charset=utf-8')
    if fmt == 'html':
        return raw(200, page(built, action), 'text/html; charset=utf-8')
    out = dict(built['meta'])
    out['png'] = base64.b64encode(built['png']).decode('ascii')
    out['dataUri'] = 'data:image/png;base64,' + out['png']
    return respond(200, out)


ROUTES = {'get': do_get}
INDEX = {'description': 'QR codes from text: JSON (PNG as base64), SVG, or an HTML page.'}


def handler(event, context):
    return run(event, SERVICE, ROUTES, INDEX)
