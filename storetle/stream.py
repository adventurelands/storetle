# stream.py — Storetle streaming format for large document collections
#
# Designed for AI training data pipelines: millions of HTML documents,
# streaming write, sequential and random-access read, self-contained files.
#
# Format (.storetle):
#   Header  : STRL(4) + version(1) + dict_size(4) + dict_bytes (usually absent)
#   Chunks  : repeated { doc_count(2) + orig_total(4) + comp_size(4)
#                        + per_doc_orig_sizes(doc_count × 4)
#                        + zstd_compressed_blob }
#   Footer  : chunk_count(8) + index_offset(8)   ← last 16 bytes
#   Index   : per_chunk → file_offset(8) + doc_count(2) + orig_total(4)
#
# Each chunk holds up to CHUNK_DOCS documents or CHUNK_BYTES uncompressed,
# whichever is reached first. All docs in a chunk are concatenated then
# compressed together with zstd level-22 + the embedded dictionary.
# This preserves cross-document redundancy within each chunk.

import struct
import os
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

from . import zstd_compat as _zs
from . import brotli_compat as _br
from .encoder import (HTMLTokenizer, _build_streams_class_split,
                     T_OPEN, T_CLOSE, T_TEXT, T_DOCTYPE,
                     T_COMMENT, T_SELFCLOSE, T_RAWTEXT)

STREAM_MAGIC   = b'STRL'
STREAM_VERSION = 2   # v2: class token splitting for class= attributes

CHUNK_DOCS  = 256        # max documents per chunk
CHUNK_BYTES = 2 << 20    # max uncompressed bytes per chunk (2MB)

ENCODE_BATCH = 128       # docs per worker task (balances IPC vs latency)

_DEFAULT_DICT_PATH = Path(__file__).parent / 'cube_dict_v10.bin'
_SRC_DIR = str(Path(__file__).parent.parent)


# ---------------------------------------------------------------------------
# Worker-process helpers (must be module-level for pickling)
# ---------------------------------------------------------------------------

def _worker_init(src_dir):
    """Called once per worker process to set up imports."""
    import sys
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)


def _encode_batch(html_list):
    """Encode a list of HTML docs in a worker process.
    Returns list of (encoded_bytes, original_byte_len)."""
    results = []
    for html in html_list:
        orig_len = len(html) if isinstance(html, bytes) else len(html.encode('utf-8', errors='replace'))
        results.append((_encode_doc(html), orig_len))
    return results


# ---------------------------------------------------------------------------
# Internal: encode/decode one HTML document (v2 format)
#
# Blob layout: ss_size(4) + struct_stream + content_stream
#
# No per-file string table — overhead exceeds savings on random web data
# (avg 10KB table per doc × 3000 docs = 30MB overhead before compression).
# Class token splitting is retained as a pure win (no overhead).
# ---------------------------------------------------------------------------

def _encode_doc(html):
    if isinstance(html, bytes):
        html = html.decode('utf-8', errors='replace')
    try:
        html.encode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        html = html.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')

    tok = HTMLTokenizer()
    tok.feed(html)
    ss, cs = _build_streams_class_split(tok.nodes)
    return struct.pack('>I', len(ss)) + ss + cs


def _encode_text_doc(text):
    """Encode a plain-text document as a single text node.

    Same container, same decoders: HTML readers see the text (escaped),
    --text / get_text() return it verbatim. This is how text-mode corpora
    (e.g. clean-text Wikipedia) are stored without any format change."""
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
    ss, cs = _build_streams_class_split([(T_TEXT, None, text)])
    return struct.pack('>I', len(ss)) + ss + cs


def _decode_doc(raw):
    """Decode a v2 blob back to reconstructed HTML bytes."""
    from .decoder import _Stream
    from .vocab import ID_TO_TAG, ID_TO_ATTR, SHARED_STRINGS
    from .folder import UNKNOWN_ID

    ss_len  = struct.unpack_from('>I', raw, 0)[0]
    ss_data = raw[4: 4 + ss_len]
    cs_data = raw[4 + ss_len:]

    ss = _Stream(ss_data)
    cs = _Stream(cs_data)

    output    = []
    indent    = 0
    tag_stack = []

    while ss._pos < len(ss_data):
        nt = ss.read_byte()

        if nt in (T_OPEN, T_SELFCLOSE):
            tag_id = ss.read_byte()
            tag    = cs.read_string(SHARED_STRINGS) if tag_id == UNKNOWN_ID \
                     else ID_TO_TAG.get(tag_id, '?%d' % tag_id)
            ac     = ss.read_byte()
            attrs  = ''
            for _ in range(ac):
                aid   = ss.read_byte()
                aname = cs.read_string(SHARED_STRINGS) if aid == UNKNOWN_ID \
                        else ID_TO_ATTR.get(aid, '?%d' % aid)
                val   = cs.read_string(SHARED_STRINGS)
                if val is None:
                    attrs += ' %s' % aname
                else:
                    e = val.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;')
                    attrs += ' %s="%s"' % (aname, e)
            output.append('%s<%s%s>' % ('  ' * indent, tag, attrs))
            if nt == T_OPEN:
                tag_stack.append(tag)
                indent += 1

        elif nt == T_CLOSE:
            tag    = tag_stack.pop() if tag_stack else ''
            indent = max(0, indent - 1)
            output.append('%s</%s>' % ('  ' * indent, tag))

        elif nt == T_TEXT:
            raw_t = cs.read_string(SHARED_STRINGS) or ''
            output.append(raw_t.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'))
        elif nt == T_RAWTEXT:
            output.append(cs.read_string(SHARED_STRINGS) or '')
        elif nt == T_DOCTYPE:
            output.append('<!%s>' % (cs.read_string(SHARED_STRINGS) or ''))
        elif nt == T_COMMENT:
            output.append('<!--%s-->' % (cs.read_string(SHARED_STRINGS) or ''))

    return '\n'.join(output).encode('utf-8')


# ---------------------------------------------------------------------------
# Compression helpers
# ---------------------------------------------------------------------------

def _compress(data: bytes, dictionary: bytes):
    if dictionary and _zs.available():
        return _zs.compress_with_dict(data, dictionary, level=22)
    return _br.compress(data)


def _decompress(data: bytes, dictionary: bytes):
    if dictionary and _zs.available():
        return _zs.decompress_with_dict(data, dictionary)
    return _br.decompress(data)


# ---------------------------------------------------------------------------
# StreamWriter
# ---------------------------------------------------------------------------

class StreamWriter:
    """Append HTML documents to a .storetle file.

    Usage:
        with StreamWriter('crawl.storetle') as w:
            w.append(html_bytes_or_str)
        print(w.stats())
    """

    def __init__(self, path, dictionary=None, embed_dict=False, workers=1):
        """
        workers=1  (default): single-threaded, ~3 MB/s.
        workers=N  (N>1):     parallel encoding via N worker processes.
                               Scales linearly with cores; use os.cpu_count()
                               for maximum throughput.

        embed_dict=False (default): dictionary is a codec parameter, not stored
            in the file. Decoder must load cube_dict_v10.bin from disk.
        embed_dict=True: dictionary bytes are stored in the file header.
        """
        self._path = Path(path)
        if dictionary is None:
            dictionary = _DEFAULT_DICT_PATH.read_bytes() \
                if _DEFAULT_DICT_PATH.exists() else b''
        self._dict       = dictionary
        self._embed_dict = embed_dict

        self._fh          = open(self._path, 'wb')
        self._chunk_buf   = []
        self._chunk_bytes = 0
        self._chunk_index = []
        self._total_docs  = 0
        self._total_orig  = 0
        self._total_comp  = 0

        # Parallel encoding state
        self._workers      = max(1, workers)
        self._executor     = None
        self._pending      = []   # list of futures, in submission order
        self._encode_buf   = []   # accumulating batch for next worker submit
        self._max_pending  = self._workers * 4  # cap in-flight futures

        if self._workers > 1:
            self._executor = ProcessPoolExecutor(
                max_workers=self._workers,
                initializer=_worker_init,
                initargs=(_SRC_DIR,),
            )

        self._write_header()

    def _write_header(self):
        stored = self._dict if self._embed_dict else b''
        self._fh.write(STREAM_MAGIC)
        self._fh.write(bytes([STREAM_VERSION]))
        self._fh.write(struct.pack('>I', len(stored)))
        if stored:
            self._fh.write(stored)

    def append(self, html):
        """Encode and buffer one HTML document."""
        if self._workers > 1:
            self._append_parallel(html)
        else:
            self._append_sync(html)

    def append_text(self, text):
        """Encode and buffer one plain-text document (no HTML parsing)."""
        if self._workers > 1:
            # preserve document order: settle in-flight HTML encodes first
            self._drain_all()
        if isinstance(text, str):
            data = text.encode('utf-8', errors='replace')
        else:
            data = text
        self._total_orig += len(data)
        raw = _encode_text_doc(data)
        self._chunk_buf.append(raw)
        self._chunk_bytes += len(raw)
        self._total_docs  += 1
        if len(self._chunk_buf) >= CHUNK_DOCS or self._chunk_bytes >= CHUNK_BYTES:
            self._flush_chunk()

    def _append_sync(self, html):
        if isinstance(html, str):
            try:
                html = html.encode('utf-8')
            except UnicodeEncodeError:
                html = html.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace').encode('utf-8')
        self._total_orig += len(html)
        raw = _encode_doc(html)
        self._chunk_buf.append(raw)
        self._chunk_bytes += len(raw)
        self._total_docs  += 1
        if len(self._chunk_buf) >= CHUNK_DOCS or self._chunk_bytes >= CHUNK_BYTES:
            self._flush_chunk()

    def _append_parallel(self, html):
        self._encode_buf.append(html)
        # Submit a batch when full
        if len(self._encode_buf) >= ENCODE_BATCH:
            self._submit_batch()
        # If we have too many in-flight futures, drain the oldest to bound memory
        if len(self._pending) >= self._max_pending:
            self._drain_one()

    def _submit_batch(self):
        if not self._encode_buf:
            return
        batch = self._encode_buf[:]
        self._encode_buf = []
        fut = self._executor.submit(_encode_batch, batch)
        self._pending.append(fut)

    def _drain_one(self):
        """Block until the oldest pending future is done, feed results."""
        if not self._pending:
            return
        fut = self._pending.pop(0)
        for encoded, orig_len in fut.result():
            self._total_orig  += orig_len
            self._chunk_buf.append(encoded)
            self._chunk_bytes += len(encoded)
            self._total_docs  += 1
            if len(self._chunk_buf) >= CHUNK_DOCS or self._chunk_bytes >= CHUNK_BYTES:
                self._flush_chunk()

    def _drain_all(self):
        """Flush encode_buf, then block until all pending futures are done."""
        self._submit_batch()
        while self._pending:
            self._drain_one()

    def _flush_chunk(self):
        if not self._chunk_buf:
            return

        doc_count  = len(self._chunk_buf)
        sizes      = [len(d) for d in self._chunk_buf]
        blob       = b''.join(self._chunk_buf)
        orig_total = sum(sizes)
        compressed = _compress(blob, self._dict)

        offset = self._fh.tell()
        self._chunk_index.append((offset, doc_count, orig_total))

        # chunk header: doc_count(2) + orig_total(4) + comp_size(4)
        self._fh.write(struct.pack('>HII', doc_count, orig_total, len(compressed)))
        # per-doc orig sizes
        self._fh.write(struct.pack(f'>{doc_count}I', *sizes))
        # compressed blob
        self._fh.write(compressed)

        self._total_comp += len(compressed)
        self._chunk_buf   = []
        self._chunk_bytes = 0

    def _write_footer(self):
        self._flush_chunk()
        index_offset = self._fh.tell()

        # Write index: one entry per chunk
        for (offset, doc_count, orig_total) in self._chunk_index:
            self._fh.write(struct.pack('>QHI', offset, doc_count, orig_total))

        # Footer: chunk_count(8) + index_offset(8)
        self._fh.write(struct.pack('>QQ', len(self._chunk_index), index_offset))

    def close(self):
        if self._workers > 1:
            self._drain_all()
            self._executor.shutdown(wait=True)
        self._write_footer()
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def stats(self):
        size = self._path.stat().st_size
        return {
            'docs':            self._total_docs,
            'chunks':          len(self._chunk_index),
            'original_bytes':  self._total_orig,
            'compressed_bytes': size,
            'ratio_pct':       round((1 - size / max(self._total_orig, 1)) * 100, 2),
        }


# ---------------------------------------------------------------------------
# StreamReader
# ---------------------------------------------------------------------------

class StreamReader:
    """Read documents from a .storetle file.

    Usage:
        with StreamReader('crawl.storetle') as r:
            print(r.doc_count)
            for html in r:          # sequential
                process(html)
            doc = r[42]             # random access
            docs = r[100:200]       # slice → list
    """

    def __init__(self, path, dictionary=None):
        self._path        = Path(path)
        self._fh          = open(self._path, 'rb')
        self._dict, self._data_start = self._read_header()
        # Override the dict loaded from file/disk if caller supplied one
        if dictionary is not None:
            self._dict = dictionary
        self._index = self._read_index()   # list of (file_offset, doc_count, orig_total)
        self.doc_count = sum(dc for _, dc, _ in self._index)

    def _read_header(self):
        magic = self._fh.read(4)
        if magic != STREAM_MAGIC:
            raise ValueError(f'Not a .storetle file (got {magic!r})')
        version = self._fh.read(1)[0]
        if version != STREAM_VERSION:
            raise ValueError('Unsupported version %d (this build writes v%d)' % (version, STREAM_VERSION))
        dict_size = struct.unpack('>I', self._fh.read(4))[0]
        if dict_size:
            dictionary = self._fh.read(dict_size)
        else:
            # Dict not embedded — load from codec installation
            dictionary = _DEFAULT_DICT_PATH.read_bytes() \
                if _DEFAULT_DICT_PATH.exists() else b''
        return dictionary, self._fh.tell()

    def _read_index(self):
        # Footer is last 16 bytes
        self._fh.seek(-16, 2)
        chunk_count, index_offset = struct.unpack('>QQ', self._fh.read(16))
        self._fh.seek(index_offset)
        index = []
        for _ in range(chunk_count):
            offset, doc_count, orig_total = struct.unpack('>QHI', self._fh.read(14))
            index.append((offset, doc_count, orig_total))
        return index

    def _read_chunk(self, chunk_idx: int):
        """Decompress a chunk and return list of raw encoded docs."""
        offset, doc_count, orig_total = self._index[chunk_idx]
        self._fh.seek(offset)
        dc2, ot2, comp_size = struct.unpack('>HII', self._fh.read(10))
        sizes      = list(struct.unpack(f'>{doc_count}I', self._fh.read(doc_count * 4)))
        compressed = self._fh.read(comp_size)
        blob       = _decompress(compressed, self._dict)

        docs = []
        pos  = 0
        for sz in sizes:
            docs.append(blob[pos: pos + sz])
            pos += sz
        return docs

    def _locate(self, doc_idx: int):
        """Return (chunk_idx, within_chunk_idx) for global doc_idx."""
        if doc_idx < 0:
            doc_idx += self.doc_count
        if not (0 <= doc_idx < self.doc_count):
            raise IndexError(f'document index {doc_idx} out of range')
        running = 0
        for ci, (_, dc, _) in enumerate(self._index):
            if doc_idx < running + dc:
                return ci, doc_idx - running
            running += dc
        raise IndexError(doc_idx)

    def get(self, doc_idx: int):
        """Return decoded HTML bytes for a single document."""
        ci, wi = self._locate(doc_idx)
        raw = self._read_chunk(ci)[wi]
        return _decode_doc(raw)

    def get_text(self, doc_idx: int):
        """Return extracted plain text (no tags) for a single document."""
        from .text import decode_text
        ci, wi = self._locate(doc_idx)
        return decode_text(self._read_chunk(ci)[wi])

    def iter_text(self):
        """Yield extracted plain text for every document, in order."""
        from .text import decode_text
        for ci in range(len(self._index)):
            for raw in self._read_chunk(ci):
                yield decode_text(raw)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.get(key)
        if isinstance(key, slice):
            return [self.get(i) for i in range(*key.indices(self.doc_count))]
        raise TypeError(f'index must be int or slice, not {type(key).__name__}')

    def __iter__(self):
        for ci in range(len(self._index)):
            for raw in self._read_chunk(ci):
                yield _decode_doc(raw)

    def iter_raw(self):
        """Yield encoded (pre-decode) document blobs, in order.

        For bulk pipelines that want to parallelize decoding themselves:
        feed these to _decode_doc / text.decode_text in worker processes.
        """
        for ci in range(len(self._index)):
            for raw in self._read_chunk(ci):
                yield raw

    def __len__(self):
        return self.doc_count

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def info(path: str):
        with StreamReader(path) as r:
            file_size = Path(path).stat().st_size
            orig_total = sum(ot for _, _, ot in r._index)
            return {
                'docs':            r.doc_count,
                'chunks':          len(r._index),
                'compressed_bytes': file_size,
                'original_bytes':  orig_total,
                'ratio_pct':       round((1 - file_size / max(orig_total, 1)) * 100, 2),
            }
