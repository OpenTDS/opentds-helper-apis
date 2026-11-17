"""PDF reading for mail/attachments/find and mail/attachments/text.

What this adds over a raw stream scan:
  * Object structure: objects (including those packed in object streams),
    the page tree, per-page content streams and resources, per-font
    decoding (ToUnicode maps, simple-font encodings, glyph widths), and
    Form XObjects, so text is attributed to the right page and font.
  * Layout: every piece of text is placed using the text and graphics
    matrices; pieces at the same height become one line, ordered left to
    right, joined by a space - or a tab where there is a column gap.
  * Standard-security decryption: RC4 40/128-bit (R2-R4), AES-128 (R4) and
    AES-256 (R5/R6), with a user or owner password. The empty password is
    tried first, so 'restricted but openable' PDFs need no password.
  * Document information (Title, Author, custom keys such as InvoiceNumber).

Standard library only (plus mail_crypto). Not a renderer: no Type3 glyph
procedures, no OCR of scanned pages.
"""
import hashlib
import re
import struct
import zlib

import mail_crypto as crypto
import mail_pdf_metrics


class PdfError(Exception):
    pass


class PasswordRequired(PdfError):
    pass


class PasswordIncorrect(PdfError):
    pass


class Unsupported(PdfError):
    pass


# ====================================================================== objects
class Ref(tuple):
    __slots__ = ()

    def __new__(cls, num, gen):
        return tuple.__new__(cls, (num, gen))


class Name(str):
    __slots__ = ()


class PStr(bytes):
    """A PDF string (literal or hex), still raw bytes."""


class Stream:
    __slots__ = ('dict', 'raw', 'num', 'gen', '_data')

    def __init__(self, d, raw, num, gen):
        self.dict, self.raw, self.num, self.gen, self._data = d, raw, num, gen, None


_WS = b'\x00\t\n\x0c\r '
_DELIM = b'()<>[]{}/%'
_NUM_RE = re.compile(rb'[+-]?(?:\d+\.?\d*|\.\d+)')
_ESC = {ord('n'): b'\n', ord('r'): b'\r', ord('t'): b'\t', ord('b'): b'\b', ord('f'): b'\f',
        ord('('): b'(', ord(')'): b')', ord('\\'): b'\\'}


def _skip_ws(buf, i):
    n = len(buf)
    while i < n:
        c = buf[i]
        if c in _WS:
            i += 1
        elif c == 0x25:  # %
            while i < n and buf[i] not in (10, 13):
                i += 1
        else:
            break
    return i


def _literal(buf, i):
    """buf[i] == '('. Returns (bytes, index after ')')."""
    out = bytearray()
    depth, i, n = 1, i + 1, len(buf)
    while i < n:
        c = buf[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            c = buf[i]
            if c in _ESC:
                out += _ESC[c]
                i += 1
            elif c in (10, 13):
                i += 1
                if c == 13 and i < n and buf[i] == 10:
                    i += 1
            elif 0x30 <= c <= 0x37:
                j = i
                while j < n and j < i + 3 and 0x30 <= buf[j] <= 0x37:
                    j += 1
                out.append(int(buf[i:j], 8) & 0xFF)
                i = j
            else:
                out.append(c)
                i += 1
        elif c == 0x28:
            depth += 1
            out.append(c)
            i += 1
        elif c == 0x29:
            depth -= 1
            i += 1
            if depth == 0:
                return bytes(out), i
            out.append(c)
        else:
            out.append(c)
            i += 1
    return bytes(out), i


def _hexstr(buf, i):
    j = buf.find(b'>', i)
    if j < 0:
        j = len(buf)
    h = re.sub(rb'[^0-9A-Fa-f]', b'', buf[i + 1:j])
    if len(h) % 2:
        h += b'0'
    return bytes.fromhex(h.decode('ascii')), j + 1


def _name(buf, i):
    j = i + 1
    n = len(buf)
    while j < n and buf[j] not in _WS and buf[j] not in _DELIM:
        j += 1
    raw = buf[i + 1:j]
    if b'#' in raw:
        raw = re.sub(rb'#([0-9A-Fa-f]{2})', lambda m: bytes([int(m.group(1), 16)]), raw)
    return Name(raw.decode('latin-1')), j


def parse_value(buf, i):
    """Parse one PDF object at buf[i]. Returns (value, next index)."""
    i = _skip_ws(buf, i)
    if i >= len(buf):
        raise PdfError('unexpected end of data')
    c = buf[i]
    if c == 0x2F:
        return _name(buf, i)
    if c == 0x28:
        s, i = _literal(buf, i)
        return PStr(s), i
    if c == 0x3C:
        if buf[i + 1:i + 2] == b'<':
            d, i = {}, i + 2
            while True:
                i = _skip_ws(buf, i)
                if buf[i:i + 2] == b'>>' or i >= len(buf):
                    return d, i + 2
                k, i = parse_value(buf, i)
                v, i = parse_value(buf, i)
                if isinstance(k, str):
                    d[str(k)] = v
        s, i = _hexstr(buf, i)
        return PStr(s), i
    if c == 0x5B:
        arr, i = [], i + 1
        while True:
            i = _skip_ws(buf, i)
            if i >= len(buf) or buf[i] == 0x5D:
                return arr, i + 1
            v, i = parse_value(buf, i)
            arr.append(v)
    m = _NUM_RE.match(buf, i)
    if m:
        tok = m.group(0)
        j = m.end()
        if b'.' not in tok:
            # possible indirect reference: <int> <int> R
            m2 = re.match(rb'\s+(\d+)\s+R(?![A-Za-z])', buf[j:j + 24])
            if m2:
                return Ref(int(tok), int(m2.group(1))), j + m2.end()
            return int(tok), j
        return float(tok), j
    j = i
    while j < len(buf) and buf[j] not in _WS and buf[j] not in _DELIM:
        j += 1
    word = buf[i:j]
    if word == b'true':
        return True, j
    if word == b'false':
        return False, j
    if word == b'null':
        return None, j
    return Name(word.decode('latin-1')), max(j, i + 1)


# ====================================================================== filters
def _png_unpredict(data, columns, colors=1, bpc=8):
    bpp = max(1, colors * bpc // 8)
    row = columns * colors * bpc // 8
    out, prev = bytearray(), bytearray(row)
    i = 0
    while i + row < len(data) + 1 and i < len(data):
        ft = data[i]
        cur = bytearray(data[i + 1:i + 1 + row])
        if len(cur) < row:
            cur += bytes(row - len(cur))
        for k in range(row):
            left = cur[k - bpp] if k >= bpp else 0
            up = prev[k]
            ul = prev[k - bpp] if k >= bpp else 0
            if ft == 1:
                cur[k] = (cur[k] + left) & 0xFF
            elif ft == 2:
                cur[k] = (cur[k] + up) & 0xFF
            elif ft == 3:
                cur[k] = (cur[k] + ((left + up) >> 1)) & 0xFF
            elif ft == 4:
                p = left + up - ul
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                cur[k] = (cur[k] + (left if pa <= pb and pa <= pc else up if pb <= pc else ul)) & 0xFF
        out += cur
        prev = cur
        i += row + 1
    return bytes(out)


def _flate(b):
    for fn in (zlib.decompress, lambda x: zlib.decompressobj().decompress(x),
               lambda x: zlib.decompress(x, -15)):
        try:
            out = fn(b)
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
    raise PdfError('flate data could not be decompressed')


def _a85(b):
    import base64
    t = re.sub(rb'\s', b'', b)
    if t.startswith(b'<~'):
        t = t[2:]
    if t.endswith(b'~>'):
        t = t[:-2]
    return base64.a85decode(t)


def _ahx(b):
    t = re.sub(rb'[^0-9A-Fa-f]', b'', b.split(b'>')[0])
    if len(t) % 2:
        t += b'0'
    return bytes.fromhex(t.decode('ascii'))


def _lzw(data):
    out, table = bytearray(), [bytes([i]) for i in range(256)] + [b'', b'']
    bits, buf, nbits, prev, size = 0, 0, 9, None, 258
    for byte in data:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= nbits:
            bits -= nbits
            code = (buf >> bits) & ((1 << nbits) - 1)
            if code == 256:
                table, nbits, prev = table[:258], 9, None
                continue
            if code == 257:
                return bytes(out)
            if prev is None:
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            else:
                entry = prev + prev[:1]
                table.append(entry)
            out += entry
            prev = entry
            if len(table) >= (1 << nbits) - 1 and nbits < 12:
                nbits += 1
    return bytes(out)


def apply_filters(data, d):
    filters = d.get('Filter')
    params = d.get('DecodeParms')
    if filters is None:
        return data
    if not isinstance(filters, list):
        filters, params = [filters], [params]
    elif not isinstance(params, list):
        params = [params] * len(filters)
    for f, p in zip(filters, params + [None] * len(filters)):
        f = str(f)
        if f in ('FlateDecode', 'Fl'):
            data = _flate(data)
            if isinstance(p, dict) and isinstance(p.get('Predictor'), int) and p['Predictor'] >= 10:
                data = _png_unpredict(data, int(p.get('Columns', 1)), int(p.get('Colors', 1)),
                                      int(p.get('BitsPerComponent', 8)))
        elif f in ('ASCII85Decode', 'A85'):
            data = _a85(data)
        elif f in ('ASCIIHexDecode', 'AHx'):
            data = _ahx(data)
        elif f in ('LZWDecode', 'LZW'):
            data = _lzw(data)
        elif f == 'Crypt':
            pass
        else:
            raise PdfError('unsupported filter ' + f)   # image codecs: never text
    return data


# ====================================================================== security
_PAD = bytes.fromhex('28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A')


class Security:
    """Standard security handler: derives the file key from a password."""

    def __init__(self, enc, file_id):
        if str(enc.get('Filter', 'Standard')) != 'Standard':
            raise Unsupported('this PDF uses the %s security handler (certificate-based); only '
                              'password protection is supported' % enc.get('Filter'))
        self.v = int(enc.get('V', 0))
        self.r = int(enc.get('R', 2))
        self.o = bytes(enc.get('O', b''))
        self.u = bytes(enc.get('U', b''))
        self.oe = bytes(enc.get('OE', b''))
        self.ue = bytes(enc.get('UE', b''))
        self.p = int(enc.get('P', 0))
        self.id0 = file_id
        self.encrypt_metadata = enc.get('EncryptMetadata', True) is not False
        self.length = int(enc.get('Length', 40)) // 8 if self.v != 1 else 5
        self.stm_method = self.str_method = 'RC4'
        if self.v >= 4:
            cf = enc.get('CF') or {}
            for attr, key in (('stm_method', 'StmF'), ('str_method', 'StrF')):
                name = str(enc.get(key, 'Identity'))
                if name == 'Identity':
                    setattr(self, attr, 'None')
                    continue
                cfm = str((cf.get(name) or {}).get('CFM', 'None'))
                setattr(self, attr, {'V2': 'RC4', 'AESV2': 'AES128', 'AESV3': 'AES256'}.get(cfm, 'None'))
                if cfm == 'V2':
                    self.length = int((cf.get(name) or {}).get('Length', self.length * 8 if self.length > 16 else self.length)) or self.length
                    if self.length > 16:
                        self.length //= 8
        if self.r not in (2, 3, 4, 5, 6):
            raise Unsupported('unsupported security revision R%d' % self.r)
        self.key = None
        self.password_kind = None

    @property
    def description(self):
        m = self.stm_method
        if m == 'RC4':
            return 'RC4-%d' % (self.length * 8)
        return {'AES128': 'AES-128', 'AES256': 'AES-256'}.get(m, 'none')

    # --- R2-R4 ---------------------------------------------------------
    def _key_r4(self, password):
        pw = (password + _PAD)[:32]
        h = hashlib.md5(pw + self.o + struct.pack('<i', self.p) + self.id0)
        if self.r >= 4 and not self.encrypt_metadata:
            h.update(b'\xff\xff\xff\xff')
        key = h.digest()[:self.length]
        if self.r >= 3:
            for _ in range(50):
                key = hashlib.md5(key).digest()[:self.length]
        return key

    def _user_ok_r4(self, key):
        if self.r == 2:
            return crypto.rc4(key, _PAD) == self.u[:32]
        x = crypto.rc4(key, hashlib.md5(_PAD + self.id0).digest())
        for i in range(1, 20):
            x = crypto.rc4(bytes(b ^ i for b in key), x)
        return x[:16] == self.u[:16]

    def _owner_to_user_r4(self, owner):
        k = hashlib.md5((owner + _PAD)[:32]).digest()
        if self.r >= 3:
            for _ in range(50):
                k = hashlib.md5(k).digest()
        k = k[:self.length]
        if self.r == 2:
            return crypto.rc4(k, self.o[:32])
        x = self.o[:32]
        for i in range(19, -1, -1):
            x = crypto.rc4(bytes(b ^ i for b in k), x)
        return x

    # --- R5/R6 ---------------------------------------------------------
    def _hash6(self, pw, salt, udata=b''):
        k = hashlib.sha256(pw + salt + udata).digest()
        if self.r == 5:
            return k
        i = 0
        while True:
            k1 = (pw + k + udata) * 64
            e = crypto.aes_cbc_encrypt(k[:16], k[16:32], k1)
            mod = sum(e[:16]) % 3
            k = (hashlib.sha256, hashlib.sha384, hashlib.sha512)[mod](e).digest()
            i += 1
            if i >= 64 and e[-1] <= i - 32:
                return k[:32]

    def _try_r6(self, password):
        pw = password[:127]
        u, o = self.u[:48], self.o[:48]
        if self._hash6(pw, u[32:40]) == u[:32]:
            k = self._hash6(pw, u[40:48])
            return crypto.aes_cbc_decrypt(k, bytes(16), self.ue[:32]), 'user'
        if self._hash6(pw, o[32:40], u) == o[:32]:
            k = self._hash6(pw, o[40:48], u)
            return crypto.aes_cbc_decrypt(k, bytes(16), self.oe[:32]), 'owner'
        return None, None

    def authenticate(self, password):
        """Sets the file key. password: bytes or None. Returns 'user'|'owner'."""
        candidates = [b''] if password is None else [password, b''] if password else [b'']
        for pw in candidates:
            if self.r >= 5:
                key, kind = self._try_r6(pw)
                if key:
                    self.key, self.password_kind = key, kind if pw else 'none'
                    return self.password_kind
                continue
            key = self._key_r4(pw)
            if self._user_ok_r4(key):
                self.key, self.password_kind = key, 'user' if pw else 'none'
                return self.password_kind
            if pw:
                upw = self._owner_to_user_r4(pw)
                key = self._key_r4(upw)
                if self._user_ok_r4(key):
                    self.key, self.password_kind = key, 'owner'
                    return 'owner'
        if password:
            raise PasswordIncorrect('the password is not correct for this PDF')
        raise PasswordRequired('this PDF is password-protected')

    def _obj_key(self, num, gen, aes):
        if self.r >= 5:
            return self.key
        h = hashlib.md5(self.key + struct.pack('<i', num)[:3] + struct.pack('<i', gen)[:2]
                        + (b'sAlT' if aes else b''))
        return h.digest()[:min(len(self.key) + 5, 16)]

    def decrypt(self, data, num, gen, method):
        if method == 'None' or not data:
            return data
        if method == 'RC4':
            return crypto.rc4(self._obj_key(num, gen, False), data)
        return crypto.aes_decrypt_pdf(self._obj_key(num, gen, True), data)


# ====================================================================== document
_OBJ_RE = re.compile(rb'(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b')
_TRAILER_RE = re.compile(rb'trailer\s*<<')
MAX_DEPTH = 12


class Document:
    def __init__(self, data, password=None):
        self.data = data
        self.offsets = {}
        for m in _OBJ_RE.finditer(data):
            self.offsets[int(m.group(1))] = (int(m.group(2)), m.end())   # last definition wins
        self.cache = {}
        self.objstm_index = None
        self.trailer = self._find_trailer()
        self.security = None
        enc = self.trailer.get('Encrypt')
        if enc is not None:
            enc_num = enc[0] if isinstance(enc, Ref) else None
            self._enc_num = enc_num
            enc = self.resolve(enc, decrypt=False)
            ids = self.trailer.get('ID') or [PStr(b'')]
            self.security = Security(enc, bytes(ids[0]) if ids else b'')
            pw = None if password is None else password.encode('utf-8')
            self.security.authenticate(pw)
        else:
            self._enc_num = None

    # --- trailer ------------------------------------------------------
    def _find_trailer(self):
        merged = {}
        for m in _TRAILER_RE.finditer(self.data):
            try:
                d, _ = parse_value(self.data, m.end() - 2)
                merged.update({k: v for k, v in d.items() if k in ('Root', 'Info', 'Encrypt', 'ID')})
            except Exception:  # noqa: BLE001
                pass
        if 'Root' not in merged:
            # cross-reference streams carry the trailer keys in their dictionary
            for num, (gen, off) in sorted(self.offsets.items(), key=lambda kv: kv[1][1]):
                head = self.data[off:off + 400]
                if b'/XRef' in head:
                    try:
                        d, _ = parse_value(self.data, off)
                        if isinstance(d, dict) and str(d.get('Type')) == 'XRef':
                            merged.update({k: v for k, v in d.items()
                                           if k in ('Root', 'Info', 'Encrypt', 'ID')})
                    except Exception:  # noqa: BLE001
                        pass
        if 'Root' not in merged:
            for num in self.offsets:
                try:
                    v = self.get(num)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(v, dict) and str(v.get('Type')) == 'Catalog':
                    merged['Root'] = Ref(num, self.offsets[num][0])
                    break
        if 'Root' not in merged:
            raise PdfError('no document catalog found')
        return merged

    # --- objects ------------------------------------------------------
    def _decrypt_strings(self, v, num, gen):
        if isinstance(v, PStr):
            return PStr(self.security.decrypt(bytes(v), num, gen, self.security.str_method))
        if isinstance(v, dict):
            return {k: self._decrypt_strings(x, num, gen) for k, x in v.items()}
        if isinstance(v, list):
            return [self._decrypt_strings(x, num, gen) for x in v]
        return v

    def get(self, num, decrypt=True):
        key = (num, decrypt)
        if key in self.cache:
            return self.cache[key]
        if num in self.offsets:
            gen, off = self.offsets[num]
            val, end = parse_value(self.data, off)
            j = _skip_ws(self.data, end)
            if isinstance(val, dict) and self.data[j:j + 6] == b'stream':
                j += 6
                if self.data[j:j + 2] == b'\r\n':
                    j += 2
                elif self.data[j:j + 1] in (b'\n', b'\r'):
                    j += 1
                length = val.get('Length')
                if isinstance(length, Ref):
                    try:
                        length = self.get(length[0], decrypt=False)
                    except Exception:  # noqa: BLE001
                        length = None
                if isinstance(length, int) and 0 <= length and \
                        self.data[j + length:j + length + 30].lstrip().startswith(b'endstream'):
                    raw = self.data[j:j + length]
                else:
                    e = self.data.find(b'endstream', j)
                    raw = self.data[j:e if e >= 0 else len(self.data)]
                    if raw.endswith(b'\r\n'):
                        raw = raw[:-2]
                    elif raw.endswith((b'\n', b'\r')):
                        raw = raw[:-1]
                if decrypt and self.security and num != self._enc_num:
                    val = self._decrypt_strings(val, num, gen)
                val = Stream(val, raw, num, gen)
            elif decrypt and self.security and num != self._enc_num:
                val = self._decrypt_strings(val, num, gen)
        else:
            val = self._from_objstm(num)
        self.cache[key] = val
        return val

    def _from_objstm(self, num):
        if self.objstm_index is None:
            self.objstm_index = {}
            for n, (gen, off) in self.offsets.items():
                if b'/ObjStm' not in self.data[off:off + 300]:
                    continue
                try:
                    s = self.get(n)
                    if not isinstance(s, Stream) or str(s.dict.get('Type')) != 'ObjStm':
                        continue
                    body = self.stream_data(s)
                    first, count = int(s.dict.get('First', 0)), int(s.dict.get('N', 0))
                    nums = [int(x) for x in body[:first].split()]
                    for k in range(min(count, len(nums) // 2)):
                        self.objstm_index[nums[2 * k]] = (body, first + nums[2 * k + 1])
                except Exception:  # noqa: BLE001
                    continue
        hit = self.objstm_index.get(num)
        if not hit:
            return None
        body, off = hit
        return parse_value(body, off)[0]

    def resolve(self, v, decrypt=True, depth=0):
        while isinstance(v, Ref) and depth < MAX_DEPTH:
            v = self.get(v[0], decrypt)
            depth += 1
        return v

    def stream_data(self, s):
        if s._data is None:
            raw = s.raw
            d = s.dict
            if self.security and s.num != self._enc_num and str(d.get('Type')) != 'XRef':
                if not (str(d.get('Type')) == 'Metadata' and not self.security.encrypt_metadata):
                    raw = self.security.decrypt(raw, s.num, s.gen, self.security.stm_method)
            dd = {k: self.resolve(v) for k, v in d.items() if k in ('Filter', 'DecodeParms')}
            s._data = apply_filters(raw, dd)
        return s._data

    # --- pages --------------------------------------------------------
    def pages(self):
        root = self.resolve(self.trailer['Root'])
        out = []

        def walk(node, inherited, depth):
            node = self.resolve(node)
            if not isinstance(node, dict) or depth > 40:
                return
            res = node.get('Resources', inherited)
            kids = self.resolve(node.get('Kids'))
            if isinstance(kids, list):
                for k in kids:
                    walk(k, res, depth + 1)
            elif str(node.get('Type', 'Page')) == 'Page' or 'Contents' in node:
                out.append((node, self.resolve(res) or {}))
        walk(root.get('Pages'), None, 0)
        return out

    def info(self):
        info = self.resolve(self.trailer.get('Info'))
        out = {}
        if isinstance(info, dict):
            for k, v in info.items():
                v = self.resolve(v)
                if isinstance(v, PStr):
                    out[k] = decode_text_string(bytes(v))
                elif isinstance(v, (int, float, Name)):
                    out[k] = str(v)
        return out


def decode_text_string(b):
    if b.startswith(b'\xfe\xff'):
        return b[2:].decode('utf-16-be', 'replace')
    if b.startswith(b'\xff\xfe'):
        return b[2:].decode('utf-16-le', 'replace')
    if b.startswith(b'\xef\xbb\xbf'):
        return b[3:].decode('utf-8', 'replace')
    return b.decode('cp1252', 'replace')


# ====================================================================== fonts
_GLYPHS = {
    'space': ' ', 'exclam': '!', 'quotedbl': '"', 'numbersign': '#', 'dollar': '$', 'percent': '%',
    'ampersand': '&', 'quotesingle': "'", 'quoteright': '’', 'quoteleft': '‘',
    'parenleft': '(', 'parenright': ')', 'asterisk': '*', 'plus': '+', 'comma': ',', 'hyphen': '-',
    'minus': '−', 'period': '.', 'slash': '/', 'zero': '0', 'one': '1', 'two': '2',
    'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'colon': ':', 'semicolon': ';', 'less': '<', 'equal': '=', 'greater': '>', 'question': '?',
    'at': '@', 'bracketleft': '[', 'backslash': '\\', 'bracketright': ']', 'asciicircum': '^',
    'underscore': '_', 'grave': '`', 'braceleft': '{', 'bar': '|', 'braceright': '}',
    'asciitilde': '~', 'bullet': '•', 'endash': '–', 'emdash': '—',
    'quotedblleft': '“', 'quotedblright': '”', 'ellipsis': '…', 'Euro': '€',
    'copyright': '©', 'registered': '®', 'trademark': '™', 'degree': '°',
    'multiply': '×', 'divide': '÷', 'section': '§', 'paragraph': '¶',
    'nbspace': ' ', 'fi': 'fi', 'fl': 'fl', 'ff': 'ff', 'ffi': 'ffi', 'ffl': 'ffl',
    'sterling': '£', 'yen': '¥', 'cent': '¢', 'dagger': '†',
    'periodcentered': '·', 'guillemotleft': '«', 'guillemotright': '»',
    'quotesinglbase': '‚', 'quotedblbase': '„', 'plusminus': '±',
    'onehalf': '½', 'onequarter': '¼', 'threequarters': '¾', 'mu': 'µ',
}


def _glyph_char(name):
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    m = re.match(r'^(?:uni|u)([0-9A-Fa-f]{4,6})', name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return ''
    base = name.split('.')[0].split('_')[0]
    if base != name:
        return _glyph_char(base)
    try:
        import unicodedata
        return unicodedata.lookup(name.replace('dieresis', ' with diaeresis'))
    except Exception:  # noqa: BLE001
        return ''


def parse_cmap(data):
    """ToUnicode CMap -> ({code: str}, code byte length)."""
    cmap, nbytes = {}, 1
    m = re.search(rb'begincodespacerange\s*<([0-9A-Fa-f]+)>', data)
    if m:
        nbytes = max(1, len(m.group(1)) // 2)

    def utf16(h):
        b = bytes.fromhex(h.decode('ascii') + ('0' if len(h) % 2 else ''))
        if len(b) % 2 == 0 and b:
            try:
                return b.decode('utf-16-be')
            except UnicodeDecodeError:
                pass
        return b.decode('latin-1')

    for block in re.findall(rb'beginbfchar(.*?)endbfchar', data, re.S):
        for a, b in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>', block):
            cmap[int(a, 16)] = utf16(b)
    for block in re.findall(rb'beginbfrange(.*?)endbfrange', data, re.S):
        for a, b, c, arr in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]*)>|\[(.*?)\])',
                                       block, re.S):
            lo, hi = int(a, 16), int(b, 16)
            if hi - lo > 65535:
                continue
            if c:
                base = utf16(c)
                if not base:
                    continue
                for k in range(hi - lo + 1):
                    try:
                        cmap[lo + k] = base[:-1] + chr(ord(base[-1]) + k)
                    except ValueError:
                        pass
            elif arr:
                for k, h in enumerate(re.findall(rb'<([0-9A-Fa-f]*)>', arr)):
                    if lo + k <= hi:
                        cmap[lo + k] = utf16(h)
    return cmap, nbytes


class Font:
    def __init__(self, doc, fd):
        self.name = str(fd.get('BaseFont', ''))
        subtype = str(fd.get('Subtype', ''))
        self.composite = subtype == 'Type0'
        self.nbytes = 2 if self.composite else 1
        self.cmap = None
        self.mapped = True
        tu = doc.resolve(fd.get('ToUnicode'))
        if isinstance(tu, Stream):
            try:
                self.cmap, nb = parse_cmap(doc.stream_data(tu))
                if self.composite:
                    self.nbytes = nb if nb in (1, 2) else 2
            except Exception:  # noqa: BLE001
                self.cmap = None
        # simple-font encoding
        self.enc = {}
        base = 'cp1252'
        enc = doc.resolve(fd.get('Encoding'))
        if isinstance(enc, Name) and str(enc) == 'MacRomanEncoding':
            base = 'mac_roman'
        if isinstance(enc, dict):
            if str(enc.get('BaseEncoding', '')) == 'MacRomanEncoding':
                base = 'mac_roman'
            diffs = doc.resolve(enc.get('Differences')) or []
            code = 0
            for item in diffs:
                if isinstance(item, int):
                    code = item
                elif isinstance(item, str):
                    ch = _glyph_char(str(item))
                    if ch:  # unknown glyph names fall back to the base encoding
                        self.enc[code] = ch
                    code += 1
        self.base = base
        # widths (thousandths of an em)
        self.widths, self.default_width = {}, 500
        if self.composite:
            desc = doc.resolve(fd.get('DescendantFonts'))
            if isinstance(desc, list) and desc:
                d0 = doc.resolve(desc[0]) or {}
                self.default_width = d0.get('DW', 1000) if isinstance(d0.get('DW'), (int, float)) else 1000
                w = doc.resolve(d0.get('W')) or []
                i = 0
                while i < len(w):
                    first = w[i]
                    nxt = doc.resolve(w[i + 1]) if i + 1 < len(w) else None
                    if isinstance(nxt, list):
                        for k, val in enumerate(nxt):
                            if isinstance(val, (int, float)):
                                self.widths[first + k] = val
                        i += 2
                    elif i + 2 < len(w):
                        for c in range(int(first), int(nxt) + 1):
                            self.widths[c] = w[i + 2]
                        i += 3
                    else:
                        break
            if not self.cmap:
                self.mapped = False
        else:
            first = fd.get('FirstChar', 0)
            ws = doc.resolve(fd.get('Widths')) or []
            if isinstance(first, int) and isinstance(ws, list) and ws:
                for k, val in enumerate(ws):
                    val = doc.resolve(val)
                    if isinstance(val, (int, float)):
                        self.widths[first + k] = val
            else:
                # Standard 14 fonts are often used without /Widths.
                self.widths = mail_pdf_metrics.widths_for(self.name) or {}
            self.default_width = 500

    def decode(self, b):
        """bytes -> [(char_string, width_thousandths, is_space)]"""
        out = []
        n = self.nbytes
        for i in range(0, len(b) - n + 1, n):
            code = int.from_bytes(b[i:i + n], 'big')
            if self.cmap is not None and code in self.cmap:
                ch = self.cmap[code]
            elif self.composite:
                ch = ''
            elif code in self.enc:
                ch = self.enc[code]
            else:
                ch = bytes([code]).decode(self.base, 'replace') if code >= 32 or code in (9,) else ''
                if ch == '�':
                    ch = ''
            out.append((ch, self.widths.get(code, self.default_width), n == 1 and code == 32))
        return out


# ====================================================================== content
def _mul(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_OPCHARS = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ*'\"0123456789"


def _content_tokens(s):
    """Yield operands (parsed values) and ('op', name) for a content stream."""
    i, n = 0, len(s)
    while True:
        i = _skip_ws(s, i)
        if i >= n:
            return
        c = s[i]
        if c in b'([</+-.0123456789' or s[i:i + 2] == b'<<':
            try:
                v, i = parse_value(s, i)
            except Exception:  # noqa: BLE001
                i += 1
                continue
            yield v
            continue
        if c in b')>]}{':
            i += 1
            continue
        j = i
        while j < n and s[j] not in _WS and s[j] not in _DELIM:
            j += 1
        if j == i:
            i += 1
            continue
        op = s[i:j]
        i = j
        if op == b'ID':  # inline image data: skip to EI
            m = re.compile(rb'\sEI(?=[\s]|$)').search(s, i + 1)
            i = m.end() if m else n
            continue
        if op in (b'true', b'false', b'null'):
            yield {b'true': True, b'false': False, b'null': None}[op]
            continue
        yield ('op', op)


class _Run:
    __slots__ = ('x', 'y', 'size', 'text', 'end')

    def __init__(self, x, y, size, text, end):
        self.x, self.y, self.size, self.text, self.end = x, y, size, text, end


class PageReader:
    def __init__(self, doc, fonts_cache, stats):
        self.doc = doc
        self.fonts_cache = fonts_cache
        self.stats = stats
        self.runs = []

    def _font(self, resources, name):
        fonts = self.doc.resolve((resources or {}).get('Font')) or {}
        ref = fonts.get(name) if isinstance(fonts, dict) else None
        key = tuple(ref) if isinstance(ref, Ref) else ('direct', id(ref), name)
        if key not in self.fonts_cache:
            fd = self.doc.resolve(ref)
            self.fonts_cache[key] = Font(self.doc, fd) if isinstance(fd, dict) else None
        return self.fonts_cache[key]

    def read(self, data, resources, ctm=_IDENT, depth=0):
        if depth > 6:
            return
        stack = []
        gs = [ctm]
        tm = tlm = _IDENT
        font, fs, tc, tw, th, tl, rise = None, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0
        operands = []

        def show(items):
            nonlocal tm
            if font is None:
                return
            text = []
            m = _mul(tm, gs[-1])
            x0, y0 = m[4], m[5]
            size = abs(fs) * (abs(m[3]) or abs(m[1]) or 1.0)
            adv = 0.0
            for it in items:
                if isinstance(it, (int, float)):
                    shift = -it / 1000.0 * fs * th
                    if it < -180 and text and not text[-1].endswith(' '):
                        text.append(' ')
                    adv += shift
                    continue
                if not isinstance(it, bytes):
                    continue
                for ch, w, is_space in font.decode(it):
                    if ch:
                        text.append(ch)
                        self.stats['chars'] += 1
                    elif font.composite:
                        self.stats['unmapped'] += 1
                    adv += (w / 1000.0 * fs + tc + (tw if is_space else 0.0)) * th
            tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + adv * tm[0], tm[5] + adv * tm[1])
            s = ''.join(text)
            if s.strip():
                m2 = _mul(tm, gs[-1])
                self.runs.append(_Run(x0, y0 + rise, size, s, m2[4]))

        for tok in _content_tokens(data):
            if not (isinstance(tok, tuple) and len(tok) == 2 and tok[0] == 'op'):
                operands.append(tok)
                if len(operands) > 64:
                    operands = operands[-64:]
                continue
            op = tok[1]
            try:
                if op == b'q':
                    gs.append(gs[-1])
                elif op == b'Q':
                    if len(gs) > 1:
                        gs.pop()
                elif op == b'cm' and len(operands) >= 6:
                    gs[-1] = _mul(tuple(float(x) for x in operands[-6:]), gs[-1])
                elif op == b'BT':
                    tm = tlm = _IDENT
                elif op == b'Tf' and len(operands) >= 2:
                    font = self._font(resources, str(operands[-2]))
                    fs = float(operands[-1])
                elif op == b'Tc' and operands:
                    tc = float(operands[-1])
                elif op == b'Tw' and operands:
                    tw = float(operands[-1])
                elif op == b'Tz' and operands:
                    th = float(operands[-1]) / 100.0
                elif op == b'TL' and operands:
                    tl = float(operands[-1])
                elif op == b'Ts' and operands:
                    rise = float(operands[-1])
                elif op in (b'Td', b'TD') and len(operands) >= 2:
                    tx, ty = float(operands[-2]), float(operands[-1])
                    if op == b'TD':
                        tl = -ty
                    tlm = _mul((1, 0, 0, 1, tx, ty), tlm)
                    tm = tlm
                elif op == b'Tm' and len(operands) >= 6:
                    tlm = tm = tuple(float(x) for x in operands[-6:])
                elif op == b'T*':
                    tlm = _mul((1, 0, 0, 1, 0, -tl), tlm)
                    tm = tlm
                elif op == b'Tj' and operands:
                    show([operands[-1]])
                elif op == b'TJ' and operands and isinstance(operands[-1], list):
                    show(operands[-1])
                elif op == b"'" and operands:
                    tlm = _mul((1, 0, 0, 1, 0, -tl), tlm)
                    tm = tlm
                    show([operands[-1]])
                elif op == b'"' and len(operands) >= 3:
                    tw, tc = float(operands[-3]), float(operands[-2])
                    tlm = _mul((1, 0, 0, 1, 0, -tl), tlm)
                    tm = tlm
                    show([operands[-1]])
                elif op == b'Do' and operands:
                    xo = self.doc.resolve((self.doc.resolve((resources or {}).get('XObject')) or {})
                                          .get(str(operands[-1])))
                    if isinstance(xo, Stream) and str(xo.dict.get('Subtype')) == 'Form':
                        mtx = xo.dict.get('Matrix')
                        m = tuple(float(x) for x in mtx) if isinstance(mtx, list) and len(mtx) == 6 else _IDENT
                        res = self.doc.resolve(xo.dict.get('Resources')) or resources
                        self.read(self.doc.stream_data(xo), res, _mul(m, gs[-1]), depth + 1)
            except (TypeError, ValueError, IndexError, PdfError):
                pass
            operands = []
        del stack


def layout(runs):
    """Runs -> lines of text, top to bottom, left to right."""
    if not runs:
        return ''
    runs = sorted(runs, key=lambda r: (-r.y, r.x))
    lines, cur, cur_y = [], [], None
    for r in runs:
        tol = max(1.0, 0.45 * min(r.size, cur[0].size if cur else r.size))
        if cur and abs(r.y - cur_y) <= tol:
            cur.append(r)
        else:
            if cur:
                lines.append(cur)
            cur, cur_y = [r], r.y
    if cur:
        lines.append(cur)
    out = []
    for line in lines:
        line.sort(key=lambda r: r.x)
        parts, prev = [], None
        for r in line:
            t = r.text
            if prev is not None:
                gap = r.x - prev.end
                size = max(prev.size, r.size, 1.0)
                if gap > 1.6 * size:
                    parts.append('\t')
                    t = t.lstrip()
                    if parts[-2:-1] and parts[-2].endswith(' '):
                        parts[-2] = parts[-2].rstrip()
                elif gap > 0.12 * size and not parts[-1].endswith(' ') and not t.startswith(' '):
                    parts.append(' ')
            parts.append(t)
            prev = r
        text = ''.join(parts)
        text = re.sub(r' {2,}', ' ', text).strip(' ')
        if text:
            out.append(text)
    return '\n'.join(out)


MAX_PAGES = 500


def extract(data, password=None, deadline=None):
    """-> (pages: [str], info: {key: str}, meta: dict). Raises PdfError subclasses."""
    doc = Document(data, password)
    stats = {'chars': 0, 'unmapped': 0}
    fonts = {}
    pages_text = []
    for page, res in doc.pages()[:MAX_PAGES]:
        reader = PageReader(doc, fonts, stats)
        contents = doc.resolve(page.get('Contents'))
        parts = contents if isinstance(contents, list) else [contents]
        chunks = []
        for p in parts:
            s = doc.resolve(p)
            if isinstance(s, Stream):
                try:
                    chunks.append(doc.stream_data(s))
                except PdfError:
                    pass
        reader.read(b'\n'.join(chunks), res)
        pages_text.append(layout(reader.runs))
    meta = {
        'extractor': 'pdf-layout',
        'pages': len(pages_text),
        'encrypted': doc.security is not None,
        'lowConfidence': bool(stats['unmapped'] and stats['unmapped'] > stats['chars']),
    }
    if doc.security is not None:
        meta['encryption'] = doc.security.description
        meta['passwordUsed'] = doc.security.password_kind
    try:
        info = doc.info()
    except Exception:  # noqa: BLE001
        info = {}
    return pages_text, info, meta


# ====================================================================== unlock
# Rewrite a password-protected PDF as an unprotected one, so a browser shows it
# without asking for the password. Every object is written out decrypted:
# strings in place, streams with their bytes decrypted but their filters
# (Flate etc.) kept. Objects packed in object streams are written as ordinary
# objects; the object streams, cross-reference streams and the /Encrypt
# dictionary are dropped, and a fresh classic xref table is written.

_NAME_OK = frozenset(b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
                     b'!"$&\'*+,-.:;=?@^_`|~')


def _w_name(n):
    raw = str(n).encode('latin-1', 'replace')
    return b'/' + b''.join(bytes([c]) if c in _NAME_OK else b'#%02X' % c for c in raw)


def _w_num(v):
    if isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            return b'0'
        s = ('%.6f' % v).rstrip('0').rstrip('.')
        return (s if s not in ('', '-0') else '0').encode('ascii')
    return str(v).encode('ascii')


def _w(v):
    """Serialise a parsed value back to PDF syntax."""
    if v is None:
        return b'null'
    if v is True:
        return b'true'
    if v is False:
        return b'false'
    if isinstance(v, Ref):
        return b'%d %d R' % (v[0], v[1])
    if isinstance(v, (int, float)):
        return _w_num(v)
    if isinstance(v, PStr):
        return b'<' + bytes(v).hex().encode('ascii') + b'>'
    if isinstance(v, Name):
        return _w_name(v)
    if isinstance(v, dict):
        return b'<<' + b''.join(_w_name(k) + b' ' + _w(x) + b'\n' for k, x in v.items()) + b'>>'
    if isinstance(v, (list, tuple)):
        return b'[' + b' '.join(_w(x) for x in v) + b']'
    if isinstance(v, bytes):
        return b'<' + v.hex().encode('ascii') + b'>'
    return _w_name(v)     # a stray keyword the parser kept as a name


def is_encrypted(data):
    """True if data is a PDF with an /Encrypt entry (password or permissions)."""
    try:
        return Document(data).security is not None
    except PasswordRequired:
        return True
    except PdfError:
        return False


def unlock(data, password=None):
    """-> bytes of the same PDF without encryption, or None if it was not
    encrypted. Raises PasswordRequired / PasswordIncorrect / Unsupported like
    extract(). An owner-password-only PDF unlocks without a password."""
    doc = Document(data, password)
    sec = doc.security
    if sec is None:
        return None

    objects = {}          # num -> (gen, value-or-Stream)
    doc._from_objstm(-1)  # builds the index of objects packed in object streams
    for num, (body, off) in (doc.objstm_index or {}).items():
        try:
            objects[num] = (0, parse_value(body, off)[0])
        except Exception:  # noqa: BLE001
            continue
    for num, (gen, _off) in doc.offsets.items():
        if num == doc._enc_num:
            continue
        try:
            val = doc.get(num)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(val, Stream):
            kind = str(val.dict.get('Type', ''))
            if kind in ('XRef', 'ObjStm'):
                continue
            raw = val.raw
            if not (kind == 'Metadata' and not sec.encrypt_metadata):
                raw = sec.decrypt(raw, num, gen, sec.stm_method)
            d = dict(val.dict)
            d['Length'] = len(raw)
            objects[num] = (gen, (d, raw))
        else:
            objects[num] = (gen, val)
    if not objects:
        raise PdfError('no objects could be read')

    out = bytearray(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
    offsets = {}
    for num in sorted(objects):
        gen, val = objects[num]
        offsets[num] = (len(out), gen)
        out += b'%d %d obj\n' % (num, gen)
        if isinstance(val, tuple):
            d, raw = val
            out += _w(d) + b'\nstream\n' + raw + b'\nendstream'
        else:
            out += _w(val)
        out += b'\nendobj\n'

    size = max(objects) + 1
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % size
    for num in range(1, size):
        if num in offsets:
            off, gen = offsets[num]
            out += b'%010d %05d n \n' % (off, gen)
        else:
            out += b'0000000000 65535 f \n'
    trailer = {'Size': size, 'Root': doc.trailer['Root']}
    if doc.trailer.get('Info') is not None:
        trailer['Info'] = doc.trailer['Info']
    if doc.trailer.get('ID'):
        trailer['ID'] = doc.trailer['ID']
    out += b'trailer\n' + _w(trailer) + b'\nstartxref\n%d\n%%%%EOF\n' % xref
    return bytes(out)
