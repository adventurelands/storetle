# zstd_compat.py — ctypes wrapper around the system zstd library
#
# Exposes:
#   compress(data, level=19)           — basic compression
#   decompress(data, max_size=...)     — basic decompression
#   train_dictionary(samples, size=)  — train a dictionary from a list of byte samples
#   compress_with_dict(data, d, lvl)  — compress using a pre-trained dictionary
#   decompress_with_dict(data, d)     — decompress using a pre-trained dictionary
#   available() → bool
#
# Falls back to brotli_compat if zstd is missing.

import ctypes, os, struct
from . import brotli_compat as _br_fallback

_OK  = False
_lib = None

def _try_load():
    global _OK, _lib
    import ctypes.util as _cu
    candidates = [c for c in (
        _cu.find_library("zstd"),            # canonical: let the OS linker locate it
        'libzstd.so.1', 'libzstd.so',        # Linux
        'libzstd.1.dylib', 'libzstd.dylib',  # macOS
        '/usr/local/lib/libzstd.dylib', '/opt/homebrew/lib/libzstd.dylib',
    ) if c]
    for p in candidates:
        # absolute paths must exist; bare names are resolved by the dynamic linker
        if os.path.exists(p) or not os.path.isabs(p):
            try:
                lib = ctypes.CDLL(p)
                # Check key symbols exist
                _ = lib.ZSTD_compressBound
                _ = lib.ZSTD_compress
                _ = lib.ZSTD_decompress
                _ = lib.ZSTD_isError
                # Wire types
                lib.ZSTD_compressBound.restype  = ctypes.c_size_t
                lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
                lib.ZSTD_compress.restype  = ctypes.c_size_t
                lib.ZSTD_compress.argtypes = [
                    ctypes.c_char_p, ctypes.c_size_t,  # dst, dstCapacity
                    ctypes.c_char_p, ctypes.c_size_t,  # src, srcSize
                    ctypes.c_int,                       # compressionLevel
                ]
                lib.ZSTD_decompress.restype  = ctypes.c_size_t
                lib.ZSTD_decompress.argtypes = [
                    ctypes.c_char_p, ctypes.c_size_t,
                    ctypes.c_char_p, ctypes.c_size_t,
                ]
                lib.ZSTD_isError.restype  = ctypes.c_uint
                lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
                lib.ZSTD_getErrorName.restype  = ctypes.c_char_p
                lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
                lib.ZSTD_getFrameContentSize.restype  = ctypes.c_uint64
                lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
                # Dict API
                lib.ZSTD_compress_usingDict.restype  = ctypes.c_size_t
                lib.ZSTD_compress_usingDict.argtypes = [
                    ctypes.c_void_p,                    # ctx (can be NULL with simple API)
                    ctypes.c_char_p, ctypes.c_size_t,
                    ctypes.c_char_p, ctypes.c_size_t,
                    ctypes.c_char_p, ctypes.c_size_t,
                    ctypes.c_int,
                ]
                lib.ZSTD_decompress_usingDict.restype  = ctypes.c_size_t
                lib.ZSTD_decompress_usingDict.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_char_p, ctypes.c_size_t,
                    ctypes.c_char_p, ctypes.c_size_t,
                    ctypes.c_char_p, ctypes.c_size_t,
                ]
                # CCtx / DCtx for dict mode
                lib.ZSTD_createCCtx.restype  = ctypes.c_void_p
                lib.ZSTD_createCCtx.argtypes = []
                lib.ZSTD_freeCCtx.restype    = ctypes.c_size_t
                lib.ZSTD_freeCCtx.argtypes   = [ctypes.c_void_p]
                lib.ZSTD_createDCtx.restype  = ctypes.c_void_p
                lib.ZSTD_createDCtx.argtypes = []
                lib.ZSTD_freeDCtx.restype    = ctypes.c_size_t
                lib.ZSTD_freeDCtx.argtypes   = [ctypes.c_void_p]
                # Training
                lib.ZDICT_trainFromBuffer.restype  = ctypes.c_size_t
                lib.ZDICT_trainFromBuffer.argtypes = [
                    ctypes.c_char_p, ctypes.c_size_t,   # dictBuffer, dictBufferCapacity
                    ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_uint,
                ]
                _lib = lib; _OK = True
                return
            except Exception:
                continue

_try_load()


def available() -> bool:
    return _OK


def _check(ret, op=''):
    if _lib.ZSTD_isError(ret):
        name = _lib.ZSTD_getErrorName(ret)
        raise RuntimeError(f'zstd {op} error: {name}')
    return ret


def compress(data: bytes, level: int = 19) -> bytes:
    if not _OK:
        return _br_fallback.compress(data)
    bound   = _lib.ZSTD_compressBound(len(data))
    out_buf = ctypes.create_string_buffer(bound)
    ret     = _lib.ZSTD_compress(out_buf, bound, data, len(data), level)
    _check(ret, 'compress')
    return out_buf.raw[:ret]


def decompress(data: bytes) -> bytes:
    if not _OK:
        return _br_fallback.decompress(data)
    # Try to get decompressed size from frame header
    content_size = _lib.ZSTD_getFrameContentSize(data, len(data))
    ZSTD_CONTENTSIZE_UNKNOWN = (1 << 64) - 1
    ZSTD_CONTENTSIZE_ERROR   = (1 << 64) - 2
    if content_size not in (ZSTD_CONTENTSIZE_UNKNOWN, ZSTD_CONTENTSIZE_ERROR):
        max_out = content_size
    else:
        max_out = max(len(data) * 20, 1 << 20)

    while True:
        out_buf = ctypes.create_string_buffer(max_out)
        ret = _lib.ZSTD_decompress(out_buf, max_out, data, len(data))
        if not _lib.ZSTD_isError(ret):
            return out_buf.raw[:ret]
        name = _lib.ZSTD_getErrorName(ret).decode()
        if 'Destination buffer is too small' in name:
            max_out *= 4
        else:
            raise RuntimeError(f'zstd decompress: {name}')


def train_dictionary(samples: list, dict_size: int = 112 * 1024) -> bytes:
    """Train a zstd dictionary from a list of byte strings.
    Returns the raw dictionary bytes (store alongside compressed data)."""
    if not _OK:
        return b''   # no dictionary without zstd
    # Concatenate all samples; build sizes array
    concat   = b''.join(samples)
    sizes    = (ctypes.c_size_t * len(samples))(*[len(s) for s in samples])
    dict_buf = ctypes.create_string_buffer(dict_size)
    ret = _lib.ZDICT_trainFromBuffer(
        dict_buf, dict_size,
        concat, sizes, len(samples),
    )
    if _lib.ZSTD_isError(ret):
        name = _lib.ZSTD_getErrorName(ret).decode()
        raise RuntimeError(f'zstd dict training failed: {name}')
    return dict_buf.raw[:ret]


def compress_with_dict(data: bytes, dictionary: bytes, level: int = 19) -> bytes:
    if not _OK or not dictionary:
        return compress(data, level)
    ctx  = _lib.ZSTD_createCCtx()
    try:
        bound   = _lib.ZSTD_compressBound(len(data))
        out_buf = ctypes.create_string_buffer(bound)
        ret = _lib.ZSTD_compress_usingDict(
            ctx, out_buf, bound, data, len(data), dictionary, len(dictionary), level,
        )
        _check(ret, 'compress_with_dict')
        return out_buf.raw[:ret]
    finally:
        _lib.ZSTD_freeCCtx(ctx)


def decompress_with_dict(data: bytes, dictionary: bytes) -> bytes:
    if not _OK or not dictionary:
        return decompress(data)
    content_size = _lib.ZSTD_getFrameContentSize(data, len(data))
    ZSTD_CONTENTSIZE_UNKNOWN = (1 << 64) - 1
    ZSTD_CONTENTSIZE_ERROR   = (1 << 64) - 2
    if content_size not in (ZSTD_CONTENTSIZE_UNKNOWN, ZSTD_CONTENTSIZE_ERROR):
        max_out = max(content_size, 1)
    else:
        max_out = max(len(data) * 20, 1 << 20)

    ctx = _lib.ZSTD_createDCtx()
    try:
        while True:
            out_buf = ctypes.create_string_buffer(max_out)
            ret = _lib.ZSTD_decompress_usingDict(
                ctx, out_buf, max_out, data, len(data), dictionary, len(dictionary),
            )
            if not _lib.ZSTD_isError(ret):
                return out_buf.raw[:ret]
            name = _lib.ZSTD_getErrorName(ret).decode()
            if 'Destination buffer is too small' in name:
                max_out *= 4
            else:
                raise RuntimeError(f'zstd decompress_with_dict: {name}')
    finally:
        _lib.ZSTD_freeDCtx(ctx)
