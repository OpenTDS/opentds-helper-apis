"""Text extraction (PDF, OOXML, text, printable strings) and search for
mail/attachments/find and mail/attachments/text. Standard library only.

PDFs go through mail_pdf (object structure, page layout, decryption); the
older raw stream scan below is kept as a fallback for files whose structure
cannot be read.

Result entries: {offset, page, matched, context, contextEscaped} plus, for
regex and boundary modes, {value, groups?, named?}.
"""
import bisect
import base64
import io
import re
import unicodedata
import zipfile
import zlib

import mail_pdf

# ---------------------------------------------------------------
# Attachment text extraction + literal search, stdlib only (no Lambda
# layer, no third-party packages).
# PDF: Flate / ASCII85+Flate / ASCIIHex+Flate content streams, literal
# and hex strings, ToUnicode CMaps for Identity-H subset fonts.
# Verified against reportlab (base-14 and embedded TrueType) and fpdf2
# output, matching pypdf's extraction on every probe.
# ---------------------------------------------------------------
CTX_DEFAULT = 50
CTX_MAX = 500
MAX_RESULTS = 50

# ---------------------------------------------------------------- PDF ----
_STREAM_RE = re.compile(rb'stream[ \t]*\r?\n?(.*?)endstream', re.S)
_TOUNI_RE = re.compile(rb'beginbfchar(.*?)endbfchar|beginbfrange(.*?)endbfrange', re.S)
_BFCHAR_RE = re.compile(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>')
_BFRANGE_RE = re.compile(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]*)>|\[(.*?)\])', re.S)

_PDF_ESCAPES = {b'n': '\n', b'r': '\r', b't': '\t', b'b': '\b',
                b'f': '\f', b'(': '(', b')': ')', b'\\': '\\'}


def _flate(b):
    for fn in (zlib.decompress,
               lambda x: zlib.decompressobj().decompress(x),
               lambda x: zlib.decompress(x, -15)):
        try:
            out = fn(b)
            if out:
                return out
        except Exception:
            pass
    return None


def _a85(b):
    # ReportLab and friends wrap Flate in ASCII85. Terminator is ~>.
    t = b.strip()
    if t.startswith(b'<~'):
        t = t[2:]
    if t.endswith(b'~>'):
        t = t[:-2]
    try:
        return base64.a85decode(t)
    except Exception:
        return None


def _ahx(b):
    t = re.sub(rb'[^0-9A-Fa-f>]', b'', b)
    if t.endswith(b'>'):
        t = t[:-1]
    if len(t) % 2:
        t += b'0'
    try:
        return bytes.fromhex(t.decode('ascii'))
    except Exception:
        return None


def _inflate(raw):
    """Undo the filter chains that actually turn up in the wild:
    Flate, ASCII85+Flate, ASCIIHex+Flate, and each of those alone."""
    if not raw:
        return None
    out = _flate(raw)
    if out:
        return out
    for pre in (_a85, _ahx):
        mid = pre(raw)
        if not mid:
            continue
        out = _flate(mid)
        if out:
            return out
        if b'Tj' in mid or b'TJ' in mid or b'beginbfchar' in mid:
            return mid
    return None


def _pdf_streams(data):
    """Every stream in the file, inflated where it is deflate-encoded."""
    out = []
    for m in _STREAM_RE.finditer(data):
        raw = m.group(1).rstrip(b'\r\n')
        dec = _inflate(raw)
        out.append(dec if dec is not None else raw)
    return out


def _utf16be_or_char(hexstr):
    b = bytes.fromhex(hexstr.decode('ascii'))
    if not b:
        return ''
    try:
        if len(b) % 2 == 0:
            return b.decode('utf-16-be')
    except Exception:
        pass
    return b.decode('latin-1', 'replace')


def _tounicode_maps(streams):
    """Merge every ToUnicode CMap found. Records conflicts rather than
    silently preferring one, so confidence can be reported honestly."""
    cmap, conflicts, found = {}, 0, 0

    def put(code, val):
        nonlocal conflicts
        if code in cmap and cmap[code] != val:
            conflicts += 1
            return
        cmap[code] = val

    for s in streams:
        if b'beginbfchar' not in s and b'beginbfrange' not in s:
            continue
        found += 1
        # only look inside the bfchar / bfrange blocks - a codespacerange
        # has the same <hex> <hex> shape and would otherwise poison the map
        chars = b''.join(re.findall(rb'beginbfchar(.*?)endbfchar', s, re.S))
        ranges = b''.join(re.findall(rb'beginbfrange(.*?)endbfrange', s, re.S))
        for m in _BFCHAR_RE.finditer(chars):
            put(int(m.group(1), 16), _utf16be_or_char(m.group(2)))
        for m in _BFRANGE_RE.finditer(ranges):
            lo, hi = int(m.group(1), 16), int(m.group(2), 16)
            if hi - lo > 65535:
                continue
            if m.group(3) is not None:
                base = m.group(3)
                if not base:
                    continue
                start = int(base[-4:], 16) if len(base) >= 4 else int(base, 16)
                for i in range(hi - lo + 1):
                    try:
                        put(lo + i, chr(start + i))
                    except ValueError:
                        pass
            elif m.group(4) is not None:
                items = re.findall(rb'<([0-9A-Fa-f]*)>', m.group(4))
                for i, it in enumerate(items):
                    if lo + i <= hi:
                        put(lo + i, _utf16be_or_char(it))
    return cmap, conflicts, found


def _pdf_literal(raw):
    """Decode a PDF literal string body (already stripped of outer parens)."""
    out, i, n = [], 0, len(raw)
    while i < n:
        c = raw[i:i + 1]
        if c == b'\\':
            nxt = raw[i + 1:i + 2]
            if nxt in _PDF_ESCAPES:
                out.append(_PDF_ESCAPES[nxt]); i += 2; continue
            if nxt in (b'\n', b'\r'):
                i += 2
                if nxt == b'\r' and raw[i:i + 1] == b'\n':
                    i += 1
                continue
            m = re.match(rb'[0-7]{1,3}', raw[i + 1:i + 4])
            if m:
                out.append(chr(int(m.group(0), 8)))
                i += 1 + len(m.group(0)); continue
            out.append(nxt.decode('latin-1', 'replace')); i += 2; continue
        out.append(c.decode('latin-1', 'replace')); i += 1
    return ''.join(out)


def _split_strings(chunk):
    """Yield ('lit'|'hex', bytes) for every string token in an operand chunk."""
    i, n = 0, len(chunk)
    while i < n:
        c = chunk[i:i + 1]
        if c == b'(':
            depth, j, esc = 1, i + 1, False
            while j < n and depth:
                ch = chunk[j:j + 1]
                if esc:
                    esc = False
                elif ch == b'\\':
                    esc = True
                elif ch == b'(':
                    depth += 1
                elif ch == b')':
                    depth -= 1
                j += 1
            yield 'lit', chunk[i + 1:j - 1]
            i = j
        elif c == b'<' and chunk[i + 1:i + 2] != b'<':
            j = chunk.find(b'>', i)
            if j < 0:
                break
            yield 'hex', chunk[i + 1:j]
            i = j + 1
        else:
            i += 1


# Content streams are tokenised in a single linear pass. An earlier
# version used one big regex whose nested quantifier backtracked
# exponentially across runs of numeric operands - twenty numbers were
# enough to hang it, and vector graphics are full of such runs.
_DELIM = b'()<>[]{}/%'
_WS = b'\x00\t\n\x0c\r '
_SHOW_OPS = (b'Tj', b'TJ', b"'", b'"')
_BREAK_OPS = (b'Td', b'TD', b'T*', b'ET', b'BT')
MAX_TEXT_CHARS = 5000000


def _tokens(s):
    """(kind, payload) for each token: lit | hex | num | name | op."""
    i, n = 0, len(s)
    while i < n:
        c = s[i:i + 1]
        if c in _WS:
            i += 1
        elif c == b'%':
            j = i
            while j < n and s[j:j + 1] not in b'\r\n':
                j += 1
            i = j
        elif c == b'(':
            depth, j, esc = 1, i + 1, False
            while j < n and depth:
                ch = s[j:j + 1]
                if esc:
                    esc = False
                elif ch == b'\\':
                    esc = True
                elif ch == b'(':
                    depth += 1
                elif ch == b')':
                    depth -= 1
                j += 1
            yield 'lit', s[i + 1:j - 1]
            i = j
        elif c == b'<':
            if s[i + 1:i + 2] == b'<':
                i += 2
            else:
                j = s.find(b'>', i)
                if j < 0:
                    return
                yield 'hex', s[i + 1:j]
                i = j + 1
        elif c == b'>':
            i += 2 if s[i + 1:i + 2] == b'>' else 1
        elif c in b'[]{}':
            i += 1
        elif c == b'/':
            j = i + 1
            while j < n and s[j:j + 1] not in _WS and s[j:j + 1] not in _DELIM:
                j += 1
            yield 'name', s[i + 1:j]
            i = j
        elif c in b'+-.0123456789':
            j = i + 1
            while j < n and s[j:j + 1] in b'+-.0123456789eE':
                j += 1
            yield 'num', s[i:j]
            i = j
        else:
            j = i
            while j < n and s[j:j + 1] not in _WS and s[j:j + 1] not in _DELIM:
                j += 1
            if j == i:
                j = i + 1
            yield 'op', s[i:j]
            i = j


def _stream_text(s, cmap, stats):
    out, pending = [], []
    for kind, body in _tokens(s):
        if kind == 'lit':
            pending.append(_pdf_literal(body))
        elif kind == 'hex':
            pending.append(_decode_hex(body, cmap, stats))
        elif kind == 'op':
            if body in _SHOW_OPS:
                if body in (b"'", b'"'):
                    out.append('\n')
                out.extend(pending)
                pending = []
            elif body in _BREAK_OPS:
                out.append('\n')
                pending = []
            else:
                pending = []
    out.extend(pending)
    return ''.join(out)


def _decode_hex(body, cmap, stats):
    h = re.sub(rb'\s', b'', body)
    if len(h) % 2:
        h += b'0'
    if cmap and len(h) % 4 == 0:
        codes = [int(h[i:i + 4], 16) for i in range(0, len(h), 4)]
        if codes and sum(1 for c in codes if c in cmap) >= max(1, len(codes) // 2):
            stats['mapped'] += len(codes)
            return ''.join(cmap.get(c, '') for c in codes)
    try:
        b = bytes.fromhex(h.decode('ascii'))
    except Exception:
        return ''
    if cmap:
        mapped = [cmap.get(x) for x in b]
        if sum(1 for m in mapped if m) >= max(1, len(b) // 2):
            stats['mapped'] += len(b)
            return ''.join(m or '' for m in mapped)
    stats['unmapped'] += len(b)
    return b.decode('latin-1', 'replace')


def extract_pdf_scan(data):
    streams = _pdf_streams(data)
    cmap, conflicts, nmaps = _tounicode_maps(streams)
    stats = {'mapped': 0, 'unmapped': 0}
    parts, size = [], 0
    for s in streams:
        if b'Tj' not in s and b'TJ' not in s:
            continue
        chunk = _stream_text(s, cmap, stats)
        parts.append(chunk)
        parts.append('\n')
        size += len(chunk) + 1
        if size > MAX_TEXT_CHARS:
            break
    text = ''.join(parts)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    total = stats['mapped'] + stats['unmapped']
    low = bool(nmaps and total and stats['unmapped'] > stats['mapped'])
    return text, {'extractor': 'pdf-text-layer', 'streams': len(streams),
                  'fontMaps': nmaps, 'mapConflicts': conflicts,
                  'lowConfidence': low}


# -------------------------------------------------------------- OOXML ----
_TAG_RE = re.compile(r'<[^>]+>')


def extract_ooxml(data):
    parts, names = [], 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if not n.lower().endswith('.xml'):
                continue
            if not (n.startswith('word/') or n.startswith('xl/')
                    or n.startswith('ppt/') or n.startswith('docProps/')):
                continue
            try:
                raw = z.read(n).decode('utf-8', 'replace')
            except Exception:
                continue
            names += 1
            raw = re.sub(r'</w:p>|</a:p>|</w:tr>', '\n', raw)
            raw = re.sub(r'<w:tab[^>]*/>', '\t', raw)
            parts.append(_TAG_RE.sub('', raw))
    text = '\n'.join(parts)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text, {'extractor': 'ooxml', 'xmlParts': names}


# --------------------------------------------------------------- text ----
def extract_text(data):
    for enc in ('utf-8', 'utf-16', 'cp1252', 'latin-1'):
        try:
            return data.decode(enc), {'extractor': 'text', 'encoding': enc}
        except Exception:
            continue
    return data.decode('latin-1', 'replace'), {'extractor': 'text', 'encoding': 'latin-1'}


def extract_binary(data):
    runs = re.findall(rb'[\x20-\x7e]{4,}', data)
    return '\n'.join(r.decode('ascii') for r in runs), {'extractor': 'printable-strings'}


class PasswordProblem(Exception):
    """Raised for encrypted PDFs: kind is 'required', 'incorrect' or 'unsupported'."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def extract_pdf(data, password=None):
    try:
        pages, info, meta = mail_pdf.extract(data, password)
    except mail_pdf.PasswordRequired as e:
        raise PasswordProblem('required', str(e))
    except mail_pdf.PasswordIncorrect as e:
        raise PasswordProblem('incorrect', str(e))
    except mail_pdf.Unsupported as e:
        raise PasswordProblem('unsupported', str(e))
    except Exception:  # noqa: BLE001 - unreadable structure: fall back to the stream scan
        if b'/Encrypt' in data:
            raise PasswordProblem('unsupported', 'this encrypted PDF could not be read')
        text, meta = extract_pdf_scan(data)
        meta['pages'] = None
        meta['pageOffsets'] = [0]
        meta['info'] = {}
        return text, meta
    offsets, parts, pos = [], [], 0
    for t in pages:
        offsets.append(pos)
        parts.append(t)
        pos += len(t) + 2
    meta['pageOffsets'] = offsets
    meta['info'] = info
    return '\n\n'.join(parts), meta


def page_of(meta, offset):
    offs = meta.get('pageOffsets')
    if not offs or meta.get('pages') is None:
        return None
    return bisect.bisect_right(offs, offset)


def metadata_text(info):
    """Document information as 'Key<TAB>Value' lines, searchable like any text."""
    return '\n'.join(f'{k}\t{v}' for k, v in info.items())


def extract(data, filename='', password=None):
    if data[:5] == b'%PDF-':
        return extract_pdf(data, password)
    if data[:2] == b'PK' and filename.lower().rsplit('.', 1)[-1] in (
            'docx', 'xlsx', 'pptx', 'odt', 'ods', 'odp', ''):
        try:
            return extract_ooxml(data)
        except Exception:
            pass
    sample = data[:4096]
    printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    if not sample or printable / max(1, len(sample)) > 0.85:
        return extract_text(data)
    return extract_binary(data)


# ------------------------------------------------------------- search ----
def _normalise(s):
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('\u00a0', ' ').replace('\u2011', '-')
    return re.sub(r'\s+', ' ', s)


def _escape(s):
    return s.encode('unicode_escape').decode('ascii')


def flex(term, exact=False):
    """Regex for literal text. Unless exact, any run of whitespace in the term
    matches any run of whitespace (spaces, tabs, line breaks) in the document."""
    if exact:
        return re.escape(term)
    return ''.join(r'\s+' if p.isspace() else re.escape(p)
                   for p in re.split(r'(\s+)', term) if p)


def find(text, term, ctx=CTX_DEFAULT, ignorecase=False, normalise=True,
         maxresults=MAX_RESULTS):
    """Literal search. Returns (count, results, relaxed_count, relaxed_results)."""
    def scan(hay, needle, keep_offsets_from):
        out, start = [], 0
        h = hay.lower() if ignorecase else hay
        nd = needle.lower() if ignorecase else needle
        while len(out) < maxresults:
            i = h.find(nd, start)
            if i < 0:
                break
            a, b = max(0, i - ctx), min(len(hay), i + len(needle) + ctx)
            out.append({'offset': i,
                        'matched': hay[i:i + len(needle)],
                        'context': hay[a:b],
                        'contextEscaped': _escape(hay[a:b])})
            start = i + max(1, len(needle))
        # true count, even beyond the cap
        total, s2 = 0, 0
        while True:
            i = h.find(nd, s2)
            if i < 0:
                break
            total += 1
            s2 = i + max(1, len(needle))
        return total, out

    count, results = scan(text, term, 0)
    rcount, rresults = 0, []
    if count == 0:
        nt, nq = _normalise(text), _normalise(term)
        h = nt.lower(); nd = nq.lower()
        s2 = 0
        while True:
            i = h.find(nd, s2)
            if i < 0:
                break
            rcount += 1
            if len(rresults) < maxresults:
                a, b = max(0, i - ctx), min(len(nt), i + len(nq) + ctx)
                rresults.append({'offset': i, 'matched': nt[i:i + len(nq)],
                                 'context': nt[a:b],
                                 'contextEscaped': _escape(nt[a:b])})
            s2 = i + max(1, len(nq))
    return count, results, rcount, rresults

# --- regex / boundary search ------------------------------------
# A caller-supplied pattern can backtrack catastrophically, and
# Python's re has no timeout, so every match run is wrapped in a
# SIGALRM guard. Lambda runs the handler on the main thread, so the
# alarm works; if it ever cannot be armed the run proceeds unguarded
# rather than failing.
EXTRACT_TIMEOUT_S = 10.0   # guards extraction, not just matching
REGEX_MAX_LEN = 2000
REGEX_TIMEOUT_S = 3.0
SCAN_CAP = 10000          # hard ceiling on matches examined


class RegexTimeout(Exception):
    pass


def _run_guarded(seconds, fn):
    armed, prev, sig = False, None, None
    try:
        import signal as sig

        def _boom(signum, frame):
            raise RegexTimeout()

        prev = sig.signal(sig.SIGALRM, _boom)
        sig.setitimer(sig.ITIMER_REAL, seconds)
        armed = True
    except Exception:
        armed = False
    try:
        return fn()
    finally:
        if armed:
            try:
                sig.setitimer(sig.ITIMER_REAL, 0)
                sig.signal(sig.SIGALRM, prev)
            except Exception:
                pass


def find_regex(text, pattern, ctx=CTX_DEFAULT, ignorecase=False,
               dotall=False, maxresults=20, kind='regex', meta=None):
    """Returns (count, results, truncated, empty).

    kind: 'literal' (no value field), 'regex' (group 1, or the whole match,
    as 'value') or 'boundaries' (group 1 stripped as 'value'; an empty
    capture is not a match - it is counted in 'empty' instead)."""
    flags = re.MULTILINE
    if ignorecase:
        flags |= re.IGNORECASE
    if dotall:
        flags |= re.DOTALL
    rx = re.compile(pattern, flags)

    def _scan():
        out, count, empty = [], 0, 0
        for m in rx.finditer(text):
            value = None
            if kind == 'boundaries':
                value = (m.group(1) or '').strip()
                if not value:
                    empty += 1
                    continue
            elif kind == 'regex':
                value = m.group(1) if m.groups() else m.group(0)
            count += 1
            if len(out) < maxresults:
                i, j = m.span()
                a, b = max(0, i - ctx), min(len(text), j + ctx)
                item = {'offset': i, 'page': page_of(meta or {}, i),
                        'matched': m.group(0),
                        'context': text[a:b],
                        'contextEscaped': _escape(text[a:b])}
                if kind != 'literal':
                    item['value'] = value
                    if m.groups():
                        item['groups'] = list(m.groups())
                    if m.groupdict():
                        item['named'] = m.groupdict()
                out.append(item)
            if count >= SCAN_CAP:
                return count, out, True, empty
        return count, out, count > len(out), empty

    return _run_guarded(REGEX_TIMEOUT_S, _scan)


def boundary_pattern(lb, rb, exact=False):
    """LoadRunner-style left/right boundary capture. Non-greedy, so it stops
    at the first right boundary. Without a right boundary the capture runs to
    the end of the cell: the next tab (column gap) or line end.

    Unless exact: whitespace in either boundary matches any whitespace, and
    whitespace between the left boundary and the value is skipped."""
    left = flex(lb, exact)
    if exact:
        if rb:
            return left + r'(.*?)' + re.escape(rb)
        return left + r'([^\r\n]*)'
    left += r'[ \t\r\n]*'
    if rb:
        return left + r'(.*?)' + flex(rb)
    return left + r'([^\t\r\n]*)'


