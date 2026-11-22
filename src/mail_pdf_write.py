"""PDF writing for mail/attachments/make and PDF attachments on mail/send.

The counterpart of mail_pdf (the reader). What it makes:
  * Real, valid PDFs: classic cross-reference table, Helvetica / Helvetica-
    Bold (standard fonts, not embedded) with WinAnsiEncoding, one Flate-
    compressed content stream per page, prose-like filler text.
  * Text injected by position, not by byte offset (splicing bytes into a PDF
    corrupts it or hides the text from readers):
        prepend    first lines of page 1
        overwrite  in place of filler lines, centred on the middle page
        append     last lines of the last page
    Each injected value starts on a line of its own and is wrapped only at
    spaces, so mail/attachments/find and text report it on that page.
  * Size two ways: 'pages' (what parsers, OCR and processing time feel) and
    'kb' (what upload limits and mail size caps feel). Filler compresses about
    3:1, so an exact size is reached with an incompressible private-data
    stream hung off the last page's /PieceInfo (the place the PDF spec gives
    applications for their own data; viewers ignore it).
  * Document information (Title, Author, Subject, any custom keys).
  * Optional password: AES-256 (V5 / R6), the strongest standard handler.
    The user password opens it; the owner password is not disclosed.
  * Deterministic: the same arguments (seed included) give the same bytes. No
    creation date is written, so nothing depends on the clock.

Standard library only (plus mail_crypto). WinAnsi covers Western European
text; characters outside it are refused rather than silently dropped, since a
test that injects a value it then cannot find is worse than a clear 400.
"""
import hashlib
import math
import random
import re
import struct
import zlib

import mail_crypto as crypto
import mail_pdf_metrics

PAPER = {'a4': (595, 842), 'letter': (612, 792)}
MARGIN = 56
FONT_SIZE = 10.5
LEADING = 14
MAX_PAGES = 500           # mail_pdf (and so find / text) reads up to 500 pages
MAX_INJECT_CHARS = 4000   # per injected value
PRODUCER = 'Test Helper API v1 (mail/attachments/make)'
INFO_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')
MAX_INFO_KEYS = 32
MAX_INFO_VALUE = 2000

_W_REG = mail_pdf_metrics.widths_for('Helvetica')
_W_BOLD = mail_pdf_metrics.widths_for('Helvetica-Bold')

WORDS = ("the quick brown fox jumps over a lazy dog time year people way day man "
         "thing woman life child world school state family student group country "
         "problem hand part place case week company system program question work "
         "government number night point home water room mother area money story "
         "fact month lot right study book eye job word business issue side kind "
         "head house service friend father power hour game line end member law car "
         "city community name president team minute idea body information back "
         "parent face level office door health person art war history party result "
         "change morning reason research girl guy moment air teacher force "
         "education foot boy age policy process music market sense nation plan "
         "college interest death course experience effort").split()


class WriteError(ValueError):
    """A request the writer cannot honour. extra: fields for the 400 body."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


# ====================================================================== text
def encode_text(s, what):
    """str -> WinAnsi bytes, lines split on newlines. Tabs become spaces;
    other control characters and anything WinAnsi lacks are refused."""
    s = s.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')
    bad = sorted({c for c in s if (ord(c) < 32 and c != '\n') or not _winansi(c)})
    if bad:
        raise WriteError("'%s' has characters a PDF with the standard fonts cannot show." % what,
                         unsupported=[('U+%04X' % ord(c)) for c in bad[:20]],
                         hint='WinAnsi (Western European) text only: letters with accents, '
                              'the euro sign and typographic quotes are fine; other scripts and '
                              'emoji are not')
    return s.encode('cp1252')


def _winansi(c):
    if c == '\n':
        return True
    try:
        b = c.encode('cp1252')
    except UnicodeEncodeError:
        return False
    return b[0] >= 32 and b[0] != 127


def _width(b, widths):
    return sum(widths.get(c, 556) for c in b) * FONT_SIZE / 1000.0


def wrap(b, maxw, widths=_W_REG):
    """WinAnsi bytes -> lines no wider than maxw, broken at spaces (a word
    wider than a whole line is broken where it must be)."""
    out = []
    for para in b.split(b'\n'):
        words = [w for w in para.split(b' ') if w]
        if not words:
            out.append(b'')
            continue
        cur = b''
        for w in words:
            while _width(w, widths) > maxw:          # one very long word
                if cur:
                    out.append(cur)
                    cur = b''
                cut = len(w)
                while cut > 1 and _width(w[:cut], widths) > maxw:
                    cut -= 1
                out.append(w[:cut])
                w = w[cut:]
            trial = cur + b' ' + w if cur else w
            if _width(trial, widths) <= maxw:
                cur = trial
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out


class _Filler:
    """Prose-like filler: sentences of 6-16 words, paragraphs of 3-8 lines
    with a blank line between."""

    def __init__(self, rng, maxw):
        self.rng = rng
        self.maxw = maxw
        self.para_left = 0
        self.sent_left = 0
        self.started = False
        self.pending = None      # the word that did not fit on the last line

    def _word(self):
        w = self.rng.choice(WORDS)
        if self.sent_left == 0:
            self.sent_left = self.rng.randint(6, 16)
            w = w.capitalize()
        self.sent_left -= 1
        if self.sent_left == 0:
            w += '.'
        return w.encode('ascii')

    def line(self):
        if self.para_left == 0:
            self.para_left = self.rng.randint(3, 8)
            if self.started:
                return b''
        self.started = True
        self.para_left -= 1
        cur, self.pending = self.pending, None
        while True:
            w = self._word()
            trial = cur + b' ' + w if cur else w
            if cur and _width(trial, _W_REG) > self.maxw:
                self.pending = w
                break
            cur = trial
        if self.para_left == 0:
            # End of paragraph: finish the sentence here and start afresh.
            self.pending = None
            self.sent_left = 0
            if not cur.endswith(b'.'):
                if _width(cur + b'.', _W_REG) > self.maxw:
                    cur = cur.rsplit(b' ', 1)[0]
                cur += b'.'
        return cur


# ====================================================================== layout
def _layout(n_pages, lines_per_page, maxw, rng, prepend, overwrite, append, title):
    """-> (pages: [[(bytes, bold)]], placement: {name: {page, lines}})."""
    top = [(l, False) for l in wrap(prepend, maxw)] if prepend is not None else []
    if title is not None:
        top += [(l, True) for l in wrap(title, maxw, _W_BOLD)] + [(b'', False)]
    mid = [(l, False) for l in wrap(overwrite, maxw)] if overwrite is not None else []
    bot = [(l, False) for l in wrap(append, maxw)] if append is not None else []
    L = lines_per_page
    mid_page = (n_pages - 1) // 2
    placement = {}
    if prepend is not None:
        placement['prepend'] = {'page': 1, 'lines': len(wrap(prepend, maxw))}
    if overwrite is not None:
        placement['overwrite'] = {'page': mid_page + 1, 'lines': len(mid)}
    if append is not None:
        placement['append'] = {'page': n_pages, 'lines': len(bot)}

    filler = _Filler(rng, maxw)
    pages = []
    for p in range(n_pages):
        t = top if p == 0 else []
        b = bot if p == n_pages - 1 else []
        m = mid if p == mid_page else []
        if len(t) + len(m) + len(b) > L:
            raise WriteError('The injected text does not fit on page %d.' % (p + 1),
                             linesNeeded=len(t) + len(m) + len(b), linesPerPage=L,
                             pages=n_pages,
                             hint='add pages (prepend goes on page 1, overwrite on the middle '
                                  'page, append on the last), or shorten the text')
        slots = [None] * L
        for i, item in enumerate(t):
            slots[i] = item
        for i, item in enumerate(b):
            slots[L - len(b) + i] = item
        if m:
            start = (L - len(m)) // 2
            start = max(len(t), min(start, L - len(b) - len(m)))
            for i, item in enumerate(m):
                slots[start + i] = item
        pages.append([s if s is not None else (filler.line(), False) for s in slots])
    return pages, placement


def _lit(b):
    return b'(' + b.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)') + b')'


def _content(lines, top_y):
    out = [b'BT', b'/F1 %s Tf' % _num(FONT_SIZE), b'%d TL' % LEADING,
           b'%d %s Td' % (MARGIN, _num(top_y))]
    bold = False
    for i, (text, is_bold) in enumerate(lines):
        if i:
            out.append(b'T*')
        if not text:
            continue
        if is_bold != bold:
            out.append(b'/F%d %s Tf' % (2 if is_bold else 1, _num(FONT_SIZE)))
            bold = is_bold
        out.append(_lit(text) + b' Tj')
    out.append(b'ET')
    return b'\n'.join(out) + b'\n'


def _num(v):
    s = ('%.3f' % v).rstrip('0').rstrip('.')
    return s.encode('ascii')


# ====================================================================== security
def _hash6(pw, salt, udata=b''):
    """ISO 32000-2 algorithm 2.B (R6)."""
    k = hashlib.sha256(pw + salt + udata).digest()
    i = 0
    while True:
        k1 = (pw + k + udata) * 64
        e = crypto.aes_cbc_encrypt(k[:16], k[16:32], k1)
        k = (hashlib.sha256, hashlib.sha384, hashlib.sha512)[sum(e[:16]) % 3](e).digest()
        i += 1
        if i >= 64 and e[-1] <= i - 32:
            return k[:32]


class _Encryptor:
    P = -4      # every permission granted; the password only guards opening

    def __init__(self, password, seed_bytes):
        pw = password.encode('utf-8')[:127]
        d = lambda tag, n: hashlib.sha256(tag + b'\0' + seed_bytes + b'\0' + pw).digest()[:n]  # noqa: E731
        self.fk = d(b'file-key', 32)
        owner = d(b'owner-password', 16).hex().encode('ascii')
        uvs, uks, ovs, oks = d(b'uvs', 8), d(b'uks', 8), d(b'ovs', 8), d(b'oks', 8)
        zero = bytes(16)
        self.u = _hash6(pw, uvs) + uvs + uks
        self.ue = crypto.aes_cbc_encrypt(_hash6(pw, uks), zero, self.fk)
        self.o = _hash6(owner, ovs, self.u) + ovs + oks
        self.oe = crypto.aes_cbc_encrypt(_hash6(owner, oks, self.u), zero, self.fk)
        perms = struct.pack('<i', self.P) + b'\xff\xff\xff\xff' + b'Tadb' + d(b'perms', 4)
        self.perms = crypto.aes_cbc_encrypt(self.fk, zero, perms)
        self._n = 0

    def __call__(self, data):
        self._n += 1
        iv = hashlib.sha256(self.fk + b'iv' + struct.pack('>Q', self._n)).digest()[:16]
        pad = 16 - len(data) % 16
        return iv + crypto.aes_cbc_encrypt(self.fk, iv, data + bytes([pad]) * pad)

    @staticmethod
    def size(n):
        return 16 + (n // 16 + 1) * 16

    def dictionary(self):
        h = lambda b: b'<' + b.hex().encode('ascii') + b'>'  # noqa: E731
        return (b'<< /Filter /Standard /V 5 /R 6 /Length 256 /P %d '
                b'/CF << /StdCF << /AuthEvent /DocOpen /CFM /AESV3 /Length 32 >> >> '
                b'/StmF /StdCF /StrF /StdCF /EncryptMetadata true '
                b'/O %s /U %s /OE %s /UE %s /Perms %s >>'
                % (self.P, h(self.o), h(self.u), h(self.oe), h(self.ue), h(self.perms)))


def _text_string(s):
    """Document-information value: ASCII as is, anything else UTF-16BE + BOM."""
    try:
        return s.encode('ascii')
    except UnicodeEncodeError:
        return b'\xfe\xff' + s.encode('utf-16-be')


# ====================================================================== build
class _Doc:
    """Object bodies, assembled into a file with a padding stream of any length."""

    def __init__(self, page_streams, info, enc, file_id, paper, with_padding):
        self.enc = enc
        self.file_id = file_id
        w, h = paper
        n = len(page_streams)
        # 1 catalog, 2 pages, 3 F1, 4 F2, 5 info, 6 encrypt (or null), 7 padding (or null),
        # then page i: 8+2i (page), 9+2i (content).
        objs = {}
        objs[1] = b'<< /Type /Catalog /Pages 2 0 R >>'
        kids = b' '.join(b'%d 0 R' % (8 + 2 * i) for i in range(n))
        objs[2] = b'<< /Type /Pages /Kids [%s] /Count %d >>' % (kids, n)
        objs[3] = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>'
        objs[4] = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>'
        parts = []
        for k, v in info.items():
            raw = _text_string(v)
            if enc:
                raw = enc(raw)
            parts.append(b'/' + k.encode('ascii') + b' <' + raw.hex().encode('ascii') + b'>')
        objs[5] = b'<< ' + b' '.join(parts) + b' >>'
        objs[6] = enc.dictionary() if enc else b'null'
        objs[7] = b'null'
        res = b'/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >>'
        for i, data in enumerate(page_streams):
            piece = (b' /PieceInfo << /TestHelper << /LastModified (D:20000101000000Z) '
                     b'/Private 7 0 R >> >>') if with_padding and i == n - 1 else b''
            objs[8 + 2 * i] = (b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] %s '
                               b'/Contents %d 0 R%s >>' % (w, h, res, 9 + 2 * i, piece))
            objs[9 + 2 * i] = (b'<< /Length %d /Filter /FlateDecode >>' % len(data), data)
        self.objs = objs
        self.with_padding = with_padding

    def assemble(self, pad_data=None, pad_spaces=0):
        objs = dict(self.objs)
        if pad_data is not None:
            objs[7] = (b'<< /Length %d%s >>' % (len(pad_data), b' ' * pad_spaces), pad_data)
        out = bytearray(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
        size = max(objs) + 1
        offsets = []
        for num in range(1, size):
            offsets.append(len(out))
            body = objs[num]
            out += b'%d 0 obj\n' % num
            if isinstance(body, tuple):
                out += body[0] + b'\nstream\n' + body[1] + b'\nendstream'
            else:
                out += body
            out += b'\nendobj\n'
        xref = len(out)
        out += b'xref\n0 %d\n0000000000 65535 f \n' % size
        for off in offsets:
            out += b'%010d 00000 n \n' % off
        fid = b'<' + self.file_id.hex().encode('ascii') + b'>'
        trailer = b'<< /Size %d /Root 1 0 R /Info 5 0 R /ID [%s %s]%s >>' % (
            size, fid, fid, b' /Encrypt 6 0 R' if self.enc else b'')
        out += b'trailer\n' + trailer + b'\nstartxref\n%d\n%%%%EOF\n' % xref
        return bytes(out)


def build(pages=None, kb=None, prepend=None, overwrite=None, append=None,
          title=None, author=None, subject=None, info=None, password=None,
          seed='0', paper='a4', max_kb=6000):
    """-> (pdf bytes, report). Text arguments are str (or None). Raises WriteError."""
    if paper not in PAPER:
        raise WriteError("'paper' must be a4 or letter.", received=paper, allowed=sorted(PAPER))
    w, h = PAPER[paper]
    maxw = w - 2 * MARGIN
    lines_per_page = int((h - 2 * MARGIN) // LEADING)
    top_y = h - MARGIN - FONT_SIZE

    inj = {}
    for name, val in (('prepend', prepend), ('overwrite', overwrite), ('append', append)):
        if val is None:
            continue
        if len(val) > MAX_INJECT_CHARS:
            raise WriteError("'%s' is too long." % name, maxCharacters=MAX_INJECT_CHARS,
                             received=len(val))
        inj[name] = encode_text(val, name + '64')
    title_b = encode_text(title, 'title') if title else None

    doc_info = {}
    for k, v in (info or {}).items():
        if not INFO_NAME_RE.match(str(k)):
            raise WriteError('Document-information keys must be letters, digits, '
                             "'.', '_' or '-', starting with a letter (64 at most).", received=k)
        if k in ('Producer',):
            raise WriteError("'Producer' is set by the service.", received=k)
        doc_info[str(k)] = v if isinstance(v, str) else str(v)
    if len(doc_info) > MAX_INFO_KEYS:
        raise WriteError('Too many document-information keys.', maxKeys=MAX_INFO_KEYS)
    for k, v in (('Title', title), ('Author', author), ('Subject', subject)):
        if v:
            doc_info[k] = v
    for k, v in doc_info.items():
        if len(v) > MAX_INFO_VALUE:
            raise WriteError("Document-information value '%s' is too long." % k,
                             maxCharacters=MAX_INFO_VALUE)
    doc_info['Producer'] = PRODUCER

    seed_bytes = str(seed).encode('utf-8')
    target = kb * 1024 if kb else None
    if target is not None and target > max_kb * 1024:
        raise WriteError("'kb' is over the cap.", maxKb=max_kb, received=kb)

    def render(n):
        rng = random.Random('pdf:' + str(seed))
        layout, placement = _layout(n, lines_per_page, maxw, rng, inj.get('prepend'),
                                    inj.get('overwrite'), inj.get('append'), title_b)
        return [zlib.compress(_content(p, top_y), 6) for p in layout], placement

    file_id = hashlib.md5(b'id\0' + seed_bytes + b'\0' + repr(
        (pages, kb, prepend, overwrite, append, title, author, subject,
         sorted(doc_info.items()), password, paper)).encode('utf-8')).digest()

    def make_doc(streams):
        enc = _Encryptor(password, seed_bytes) if password else None
        if enc:
            streams = [enc(s) for s in streams]
        return _Doc(streams, doc_info, enc, file_id, PAPER[paper], target is not None)

    # ---- page count
    if pages is not None:
        n = max(1, min(int(pages), MAX_PAGES))
        streams, placement = render(n)
    elif target is None:
        n = 1
        streams, placement = render(n)
    else:
        one, _ = render(1)
        per_page = len(one[0]) + (_Encryptor.size(len(one[0])) - len(one[0]) if password else 0) + 130
        n = max(1, min(MAX_PAGES, int((target * 0.9 - 2500) // per_page)))
        for _ in range(6):
            streams, placement = render(n)
            base = len(make_doc_dry(streams, password, doc_info, file_id, paper))
            if base <= target or n == 1:
                break
            n = max(1, int(n * (target - 2500) / max(base - 2500, 1)) - 1)

    doc = make_doc(streams)
    if target is None:
        data = doc.assemble()
        padding = 0
    else:
        data, padding = _exact(doc, target, bool(password), seed_bytes)
    report = {'pages': n, 'bytes': len(data), 'paddingBytes': padding,
              'encrypted': bool(password), 'paper': paper, 'linesPerPage': lines_per_page,
              'placement': placement, 'info': {k: v for k, v in doc_info.items()}}
    if password:
        report['encryption'] = 'AES-256'
    return data, report


def make_doc_dry(streams, password, doc_info, file_id, paper):
    """Size of the document without padding, without doing the encryption:
    every encrypted stream and string is replaced by zeros of its encrypted
    length (the key derivation alone takes a moment in pure Python)."""

    class _Dry:
        def __call__(self, data):
            return bytes(_Encryptor.size(len(data)))

        def dictionary(self):
            return b'<< /Filter /Standard /V 5 /R 6 /Length 256 /P -4 ' + b'x' * 600 + b' >>'

    enc = _Dry() if password else None
    s = [enc(x) for x in streams] if enc else streams
    return _Doc(s, doc_info, enc, file_id, PAPER[paper], True).assemble(b'')


def _exact(doc, target, encrypted, seed_bytes):
    """Assemble with a padding stream that brings the file to exactly target bytes."""
    stored = (lambda n: _Encryptor.size(n)) if encrypted else (lambda n: n)
    base = len(doc.assemble(bytes(stored(0))))
    if base > target:
        raise WriteError('The document is already larger than the size asked for.',
                         minKb=math.ceil(base / 1024), bytes=base,
                         hint="ask for more 'kb' or fewer pages")
    # Search on lengths with placeholder bytes (cheap), then make the real padding once.
    n, spaces = max(0, target - base - 32 if encrypted else target - base), 0
    for _ in range(64):
        size = len(doc.assemble(bytes(stored(n)), spaces))
        d = target - size
        if d == 0:
            break
        if 0 < d < 48:
            spaces += d
        else:
            n = max(0, n + d)
            if n == 0 and d < 0:
                spaces = max(0, spaces + d)
    else:
        raise WriteError('Could not reach the exact size asked for.', bytes=size)
    rnd = random.Random('pad:' + seed_bytes.decode('utf-8', 'replace'))
    pad = rnd.randbytes(n)
    if encrypted:
        pad = doc.enc(pad)
    data = doc.assemble(pad, spaces)
    if len(data) != target:      # the placeholder had the same length, so this cannot happen
        raise WriteError('Size check failed after padding.', bytes=len(data), target=target)
    return data, len(pad)
