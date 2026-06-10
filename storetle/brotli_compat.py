# brotli_compat.py — thin ctypes wrapper around the system brotli library
#
# Exposes compress(data, quality=11) and decompress(data) with the same
# calling convention as zlib.compress / zlib.decompress so callers can
# swap one for the other with a single import change.
#
# Falls back to zlib if the brotli dylib is not present (other machines).

import ctypes, zlib, os

_BROTLI_OK   = False
_enc = None
_dec = None

def _try_load():
    global _BROTLI_OK, _enc, _dec
    candidates = [
        '/usr/local/lib/libbrotlienc.dylib',
        '/usr/local/Cellar/brotli/1.2.0/lib/libbrotlienc.dylib',
        'libbrotlienc.so',
        'libbrotlienc.dylib',
    ]
    dec_candidates = [p.replace('enc', 'dec') for p in candidates]

    for ep, dp in zip(candidates, dec_candidates):
        if os.path.exists(ep) and os.path.exists(dp):
            try:
                e = ctypes.CDLL(ep)
                d = ctypes.CDLL(dp)
                # wire up encoder
                e.BrotliEncoderMaxCompressedSize.restype  = ctypes.c_size_t
                e.BrotliEncoderMaxCompressedSize.argtypes = [ctypes.c_size_t]
                e.BrotliEncoderCompress.restype  = ctypes.c_int
                e.BrotliEncoderCompress.argtypes = [
                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_size_t, ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_size_t), ctypes.c_char_p,
                ]
                # wire up decoder
                d.BrotliDecoderDecompress.restype  = ctypes.c_int
                d.BrotliDecoderDecompress.argtypes = [
                    ctypes.c_size_t, ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_size_t), ctypes.c_char_p,
                ]
                _enc = e; _dec = d; _BROTLI_OK = True
                return
            except Exception:
                continue

_try_load()


def compress(data: bytes, quality: int = 11, lgwin: int = 24, mode: int = 0) -> bytes:
    """Compress data with brotli (quality 0-11, mode 0=generic 1=text 2=font).
    Falls back to zlib level 9."""
    if not _BROTLI_OK:
        return zlib.compress(data, level=9)

    max_out  = _enc.BrotliEncoderMaxCompressedSize(len(data))
    out_buf  = ctypes.create_string_buffer(max_out)
    out_size = ctypes.c_size_t(max_out)

    ok = _enc.BrotliEncoderCompress(
        quality, lgwin, mode,
        len(data), data,
        ctypes.byref(out_size), out_buf,
    )
    if not ok:
        raise RuntimeError('brotli compression failed')
    return out_buf.raw[:out_size.value]


def decompress(data: bytes) -> bytes:
    """Decompress brotli data. Falls back to zlib if brotli unavailable."""
    if not _BROTLI_OK:
        return zlib.decompress(data)

    # Grow output buffer until it fits (decompressed size is unknown).
    max_out = max(len(data) * 10, 1 << 20)   # start at 10× or 1 MB
    while True:
        out_buf  = ctypes.create_string_buffer(max_out)
        out_size = ctypes.c_size_t(max_out)
        result   = _dec.BrotliDecoderDecompress(
            len(data), data,
            ctypes.byref(out_size), out_buf,
        )
        if result == 1:   # BROTLI_DECODER_RESULT_SUCCESS
            return out_buf.raw[:out_size.value]
        if result == 3:   # BROTLI_DECODER_RESULT_NEEDS_MORE_OUTPUT
            max_out *= 4
            continue
        raise RuntimeError(f'brotli decompression failed (result={result})')


def available() -> bool:
    return _BROTLI_OK
