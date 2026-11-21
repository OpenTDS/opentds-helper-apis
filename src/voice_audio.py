"""voice_audio - the little audio handling voice needs, in pure Python.

Polly returns raw 16-bit mono PCM with no header, so WAV is that plus a RIFF
header written here. Durations are read from the audio itself: exact for
PCM, WAV, mu-law and A-law; by walking the frames for MP3; from the last
page's sample position for Ogg.
"""
import struct

# format -> (Polly OutputFormat, content type, file extension, valid Hz, default Hz)
# A default of None means "by engine": 24000 for neural/generative, 22050 for standard.
FORMATS = {
    'mp3':   ('mp3', 'audio/mpeg', 'mp3', (8000, 16000, 22050, 24000), None),
    'ogg':   ('ogg_vorbis', 'audio/ogg', 'ogg', (8000, 16000, 22050, 24000), None),
    'wav':   ('pcm', 'audio/wav', 'wav', (8000, 16000), 16000),
    'pcm':   ('pcm', 'audio/pcm', 'pcm', (8000, 16000), 16000),
    'mulaw': ('mulaw', 'audio/basic', 'ulaw', (8000,), 8000),
    'alaw':  ('alaw', 'audio/x-alaw-basic', 'alaw', (8000,), 8000),
}

CONTENT_TYPES = {'wav': 'audio/wav', 'mp3': 'audio/mpeg', 'ogg': 'audio/ogg', 'flac': 'audio/flac',
                 'webm': 'audio/webm', 'm4a': 'audio/mp4', 'amr': 'audio/amr', 'pcm': 'audio/pcm',
                 'mulaw': 'audio/basic', 'alaw': 'audio/x-alaw-basic'}
EXTENSIONS = {'mulaw': 'ulaw'}

# Listening. A transcriber bills by the second of audio, so the only audio
# handed to it is audio whose length was COUNTED here from the bytes that will
# be decoded: uncompressed samples (so many bytes are so many seconds) and MP3
# frames (each a fixed length of sound, and never smaller than its bit rate
# says). The compressed containers below only STATE a length, and can pack hours
# of silence into a few kilobytes; on an endpoint with no login that is a way
# to run up someone else's bill, so they are recognised and refused.
LISTEN_HZ = (8000, 48000)
NOT_COUNTABLE = ('ogg', 'flac', 'webm', 'm4a', 'amr')


def default_hz(fmt, engine):
    d = FORMATS[fmt][4]
    return d if d else (22050 if engine == 'standard' else 24000)


def wav(pcm, hz):
    """16-bit mono little-endian PCM -> a WAV file."""
    return (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, hz, hz * 2, 2, 16) +
            b'data' + struct.pack('<I', len(pcm)) + pcm)


def silence(ms, hz):
    """Digital silence as 16-bit mono PCM."""
    return b'\x00\x00' * int(hz * max(0, ms) // 1000)


def _wav_parts(data):
    """(fmt tuple, data offset, data length) of a WAV file, or None. The fmt
    tuple is (tag, channels, hz, byte rate, block align, bits); for the
    'extensible' form the tag is the real one, from its SubFormat."""
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid, clen = data[pos:pos + 4], struct.unpack_from('<I', data, pos + 4)[0]
        if cid == b'fmt ' and clen >= 16 and pos + 24 <= len(data):
            fmt = struct.unpack_from('<HHIIHH', data, pos + 8)
            if fmt[0] == 0xfffe and clen >= 40 and pos + 48 <= len(data):
                fmt = (struct.unpack_from('<H', data, pos + 32)[0],) + fmt[1:]
        elif cid == b'data':
            if not fmt or not fmt[3]:
                return None
            return fmt, pos + 8, min(clen, len(data) - pos - 8)
        pos += 8 + clen + (clen & 1)
    return None


def wav_info(data):
    """(hz, channels, bits, duration ms) of a PCM WAV file, or None."""
    parts = _wav_parts(data)
    if not parts:
        return None
    (tag, channels, hz, byte_rate, _align, bits), _offset, size = parts
    if tag in (1, 3, 6, 7) and hz and channels and bits >= 8:
        byte_rate = hz * channels * (bits // 8)     # uncompressed: counted, not as stated
    return hz, channels, bits, int(round(size * 1000.0 / byte_rate))


def lengthen_wav(data, min_ms):
    """A plain PCM WAV shorter than min_ms, with silence added to reach it;
    anything else unchanged. (A transcriber refuses very short clips, and a
    one-word reply is exactly what a test needs to hear.)"""
    parts = _wav_parts(data)
    if not parts or parts[0][0] != 1:
        return data
    fmt, offset, size = parts
    _tag, channels, hz, byte_rate, align, bits = fmt
    missing = int(byte_rate * min_ms / 1000.0) - size
    if missing <= 0 or not align:
        return data
    body = data[offset:offset + size] + b'\x00' * (missing - missing % align + align)
    return (b'RIFF' + struct.pack('<I', 36 + len(body)) + b'WAVE' +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, hz, byte_rate, align, bits) +
            b'data' + struct.pack('<I', len(body)) + body)


_FLIP_SIGN = bytes(b ^ 0x80 for b in range(256))


def wav_plain(data):
    """Any uncompressed WAV -> (the same sound as a minimal 16-bit PCM WAV, hz,
    channels, ms). 8-, 24- and 32-bit samples and telephone WAVs (mu-law,
    A-law) all become 16-bit: the one form every transcriber is sure to take.
    The header is written here from what was read, so the length in ms is a
    count of the samples that are actually there.

    Raises ValueError(why) for a WAV that is compressed, or outside what a
    transcriber takes."""
    parts = _wav_parts(data)
    if not parts:
        raise ValueError('it is not a readable WAV file (no format or no audio data in it)')
    (tag, channels, hz, _rate, _align, bits), offset, size = parts
    body = data[offset:offset + size]
    if tag in (6, 7):
        if bits != 8:
            raise ValueError('its mu-law / A-law samples are not 8-bit')
        body, bits = g711_to_pcm(body, 'alaw' if tag == 6 else 'mulaw'), 16
    elif tag == 3:
        raise ValueError('its samples are floating point; save it as 16-bit PCM')
    elif tag != 1:
        raise ValueError('it is compressed (WAV format %d); save it as 16-bit PCM' % tag)
    elif bits not in (8, 16, 24, 32):
        raise ValueError('its samples are %d-bit; save it as 16-bit PCM' % bits)
    if channels not in (1, 2):
        raise ValueError('it has %d channels; mix it down to mono or stereo' % channels)
    if not LISTEN_HZ[0] <= hz <= LISTEN_HZ[1]:
        raise ValueError('its sample rate is %d Hz; resample it to between %d and %d'
                         % ((hz,) + LISTEN_HZ))
    width = bits // 8
    body = body[:len(body) - len(body) % (channels * width)]
    if not body:
        raise ValueError('it holds no audio')
    if width != 2:
        # To 16-bit by slicing (C speed): keep the two most significant bytes
        # of each sample; 8-bit WAV is unsigned, so its sign is flipped.
        pcm = bytearray(len(body) // width * 2)
        if width == 1:
            pcm[1::2] = body.translate(_FLIP_SIGN)
        else:
            pcm[0::2] = body[width - 2::width]
            pcm[1::2] = body[width - 1::width]
        body = bytes(pcm)
    align = channels * 2
    out = (b'RIFF' + struct.pack('<I', 36 + len(body)) + b'WAVE' +
           b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, hz, hz * align, align, 16) +
           b'data' + struct.pack('<I', len(body)) + body)
    return out, hz, channels, int(round(len(body) * 1000.0 / (hz * align)))


def _g711_tables():
    mu, a = [], []
    for b in range(256):
        u = ~b & 0xff
        sample = (((u & 0x0f) << 3) + 0x84) << ((u >> 4) & 7)
        sample -= 0x84
        mu.append(-sample if u & 0x80 else sample)
        x = b ^ 0x55
        exponent, mantissa = (x >> 4) & 7, x & 0x0f
        sample = (mantissa << 4) + 8 if exponent == 0 else ((mantissa << 4) + 0x108) << (exponent - 1)
        a.append(sample if x & 0x80 else -sample)
    # Each law as two 256-byte tables - the low and the high byte of every
    # sample - so a whole recording is decoded by bytes.translate, not a loop.
    return tuple((bytes(v & 0xff for v in t), bytes((v >> 8) & 0xff for v in t)) for t in (mu, a))


_MULAW_PCM, _ALAW_PCM = _g711_tables()


def g711_to_pcm(data, law):
    """Telephone audio (8-bit mu-law or A-law) -> 16-bit little-endian PCM."""
    low, high = _MULAW_PCM if law == 'mulaw' else _ALAW_PCM
    out = bytearray(2 * len(data))
    out[0::2] = data.translate(low)
    out[1::2] = data.translate(high)
    return bytes(out)


_BITRATE_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BITRATE_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_MP3_HZ = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame(data, i):
    """True when a valid MPEG Layer III frame header starts at i."""
    if i + 4 > len(data) or data[i] != 0xff or (data[i + 1] & 0xe0) != 0xe0:
        return False
    b1, b2 = data[i + 1], data[i + 2]
    return ((b1 >> 3) & 3) != 1 and ((b1 >> 1) & 3) == 1 and (b2 >> 4) not in (0, 15) \
        and ((b2 >> 2) & 3) != 3


MP3_RESYNC_MAX = 20000


def mp3_clean(data):
    """An MP3 as nothing but its audio frames -> (frames, ms, hz), or None when
    there are none.

    Tags, padding and the encoder's own summary frame (Xing / Info / VBRI) are
    left out: they describe the audio, and what they say about its length need
    not be true. What comes back is exactly what a decoder will play, and ms is
    counted from it frame by frame."""
    i, n = 0, len(data)
    out, seconds, first_hz, chained, skips = [], 0.0, None, True, 0
    while i + 4 <= n:
        if data[i:i + 3] == b'ID3' and i + 10 <= n and data[i + 3] != 0xff:
            # ID3v2: at the start, or wherever a stream was joined from chunks
            i += 10 + ((data[i + 6] & 0x7f) << 21 | (data[i + 7] & 0x7f) << 14 |
                       (data[i + 8] & 0x7f) << 7 | (data[i + 9] & 0x7f)) \
                + (10 if data[i + 5] & 0x10 else 0)
            chained = True
            continue
        length = _mp3_length(data, i)
        ok = length and i + length <= n
        if ok and not chained:
            # Found by searching, not by following the last frame: believe it
            # only if another frame (or the end) follows.
            ok = i + length == n or bool(_mp3_length(data, i + length))
        if not ok:
            skips += 1
            if skips > MP3_RESYNC_MAX:
                return None
            nxt = data.find(b'\xff', i + 1)
            if nxt < 0:
                break
            i, chained = nxt, False
            continue
        b1, b2 = data[i + 1], data[i + 2]
        version = (b1 >> 3) & 3
        hz = _MP3_HZ[version][(b2 >> 2) & 3]
        head = data[i + 4:i + 48]
        if not out and (b'Xing' in head or b'Info' in head or b'VBRI' in head):
            i, chained = i + length, True       # the summary frame: no sound in it
            continue
        out.append(data[i:i + length])
        seconds += (1152 if version == 3 else 576) / float(hz)
        first_hz = first_hz or hz
        i, chained = i + length, True
    if not out:
        return None
    return b''.join(out), int(round(seconds * 1000)), first_hz


def _mp3_length(data, i):
    """Bytes in the MPEG Layer III frame whose header starts at i, or 0."""
    if not _mp3_frame(data, i):
        return 0
    b1, b2 = data[i + 1], data[i + 2]
    version = (b1 >> 3) & 3
    hz = _MP3_HZ[version][(b2 >> 2) & 3]
    kbps = (_BITRATE_V1 if version == 3 else _BITRATE_V2)[b2 >> 4]
    length = (144 if version == 3 else 72) * kbps * 1000 // hz + ((b2 >> 1) & 1)
    return length if length >= 24 else 0


def mp3_ms(data):
    """Duration of an MPEG Layer III stream by walking its frames, or None."""
    i, n = 0, len(data)
    if data[:3] == b'ID3' and n > 10:
        i = 10 + ((data[6] & 0x7f) << 21 | (data[7] & 0x7f) << 14 |
                  (data[8] & 0x7f) << 7 | (data[9] & 0x7f))
    seconds, frames = 0.0, 0
    while i + 4 <= n:
        b1, b2 = data[i + 1], data[i + 2]
        version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        bitrate, rate = b2 >> 4, (b2 >> 2) & 3
        if (data[i] != 0xff or (b1 & 0xe0) != 0xe0 or version == 1 or layer != 1 or
                bitrate in (0, 15) or rate == 3):
            if frames:
                break               # a trailing tag, or the end of the audio
            i += 1                  # still looking for the first frame
            continue
        hz = _MP3_HZ[version][rate]
        kbps = (_BITRATE_V1 if version == 3 else _BITRATE_V2)[bitrate]
        length = (144 if version == 3 else 72) * kbps * 1000 // hz + ((b2 >> 1) & 1)
        if length < 4:
            break
        seconds += (1152 if version == 3 else 576) / float(hz)
        frames += 1
        i += length
    return int(round(seconds * 1000)) if frames else None


def ogg_ms(data):
    """Duration of an Ogg Vorbis or Opus stream from its last page, or None."""
    last = data.rfind(b'OggS')
    if last < 0 or last + 14 > len(data):
        return None
    granule = struct.unpack_from('<q', data, last + 6)[0]
    head = data[:4096]
    i = head.find(b'\x01vorbis')
    if i >= 0 and i + 16 <= len(head) and granule > 0:
        hz = struct.unpack_from('<I', head, i + 12)[0]
        return int(round(granule * 1000.0 / hz)) if hz else None
    j = head.find(b'OpusHead')
    if j >= 0 and j + 12 <= len(head) and granule > 0:
        skip = struct.unpack_from('<H', head, j + 10)[0]
        return int(round(max(0, granule - skip) / 48.0))
    return None


def duration_ms(data, fmt, hz=None):
    """Milliseconds of audio, or None when it cannot be read from the bytes."""
    try:
        if fmt == 'pcm' and hz:
            return int(round(len(data) * 500.0 / hz))
        if fmt in ('mulaw', 'alaw'):
            return int(round(len(data) * 1000.0 / (hz or 8000)))
        if fmt == 'wav':
            info = wav_info(data)
            return info[3] if info else None
        if fmt == 'mp3':
            return mp3_ms(data)
        if fmt == 'ogg':
            return ogg_ms(data)
    except (struct.error, IndexError, ZeroDivisionError):
        pass
    return None


def sniff(data, filename=''):
    """What an uploaded recording is, from its first bytes (then its name).
    -> (format, content type, extension). Unknown audio is 'bin'."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        fmt = 'wav'
    elif data[:4] == b'OggS':
        fmt = 'ogg'
    elif data[:4] == b'fLaC':
        fmt = 'flac'
    elif data[:4] == b'\x1a\x45\xdf\xa3':
        fmt = 'webm'
    elif data[4:8] == b'ftyp':
        fmt = 'm4a'
    elif data[:5] == b'#!AMR':
        fmt = 'amr'
    elif data[:3] == b'ID3':
        fmt = 'mp3'
    elif ext in ('pcm', 'raw'):          # headerless: only the name can say so
        fmt = 'pcm'
    elif ext in ('ulaw', 'mulaw'):
        fmt = 'mulaw'
    elif ext == 'alaw':
        fmt = 'alaw'
    elif _mp3_frame(data, 0):
        fmt = 'mp3'
    else:
        return 'bin', 'application/octet-stream', ext or 'bin'
    return fmt, CONTENT_TYPES[fmt], EXTENSIONS.get(fmt, fmt)
