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
                 'webm': 'audio/webm', 'm4a': 'audio/mp4', 'pcm': 'audio/pcm',
                 'mulaw': 'audio/basic', 'alaw': 'audio/x-alaw-basic'}
EXTENSIONS = {'mulaw': 'ulaw'}


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


def wav_info(data):
    """(hz, channels, bits, duration ms) of a PCM WAV file, or None."""
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    pos, fmt, size = 12, None, None
    while pos + 8 <= len(data):
        cid, clen = data[pos:pos + 4], struct.unpack_from('<I', data, pos + 4)[0]
        if cid == b'fmt ' and clen >= 16:
            fmt = struct.unpack_from('<HHIIHH', data, pos + 8)
        elif cid == b'data':
            size = min(clen, len(data) - pos - 8)
            break
        pos += 8 + clen + (clen & 1)
    if not fmt or size is None or not fmt[3]:
        return None
    _tag, channels, hz, byte_rate, _align, bits = fmt
    return hz, channels, bits, int(round(size * 1000.0 / byte_rate))


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
