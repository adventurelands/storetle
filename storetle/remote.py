# remote.py — read .storetle archives over HTTP(S) with Range requests.
#
# Opens an archive with at most three small requests (footer+index tail,
# header, dictionary if embedded), then fetches exactly one chunk span
# (≤ ~2 MB compressed) per document access. Works against any server or
# object store that honors Range (S3, R2, GitHub Pages, nginx, ...).
#
# Stdlib only — urllib, struct.

import struct
import urllib.request
from pathlib import Path

from .stream import STREAM_MAGIC, STREAM_VERSION, _decompress, _decode_doc

_DEFAULT_DICT_PATH = Path(__file__).parent / 'cube_dict_v10.bin'

# One speculative tail fetch usually captures index + footer in a single
# round trip (index entries are 14 bytes; 64 KB covers ~4,600 chunks ≈
# 1.2M documents).
_TAIL_BYTES = 64 * 1024


class RemoteReader:
    """Random-access reader for a .storetle file served over HTTP(S).

    Usage:
        with RemoteReader('https://host/corpus.storetle') as r:
            print(r.doc_count)
            html = r[42]
            for doc in r:
                ...
    """

    def __init__(self, url, dictionary=None, timeout=30):
        self._url = url
        self._timeout = timeout
        self._chunk_cache = (None, None)   # (chunk_idx, [decoded raw docs])
        self.bytes_fetched = 0

        tail = self._fetch_suffix(_TAIL_BYTES)
        if len(tail) < 16:
            raise ValueError('File too small to be a .storetle archive')
        chunk_count, index_offset = struct.unpack('>QQ', tail[-16:])

        index_size = chunk_count * 14
        if index_size + 16 <= len(tail):
            index_raw = tail[-(index_size + 16):-16]
        else:
            index_raw = self._fetch(index_offset, index_offset + index_size - 1)

        self._index = []
        for i in range(chunk_count):
            off, dc, orig = struct.unpack_from('>QHI', index_raw, i * 14)
            self._index.append((off, dc, orig))

        # chunk i occupies [offset_i, offset_{i+1}); the last ends at the index
        self._chunk_ends = [self._index[i + 1][0] for i in range(chunk_count - 1)]
        self._chunk_ends.append(index_offset)

        # cumulative doc counts for index lookup
        self._cum = [0]
        for _, dc, _ in self._index:
            self._cum.append(self._cum[-1] + dc)
        self.doc_count = self._cum[-1]

        head = self._fetch(0, 8)
        if head[:4] != STREAM_MAGIC:
            raise ValueError('Not a .storetle file (magic: %r)' % head[:4])
        if head[4] != STREAM_VERSION:
            raise ValueError('Unsupported version %d (reader is v%d)'
                             % (head[4], STREAM_VERSION))
        dict_size = struct.unpack('>I', head[5:9])[0]

        if dictionary is not None:
            self._dict = dictionary
        elif dict_size:
            self._dict = self._fetch(9, 9 + dict_size - 1)
        else:
            self._dict = _DEFAULT_DICT_PATH.read_bytes() \
                if _DEFAULT_DICT_PATH.exists() else b''

    # -- HTTP plumbing ------------------------------------------------------

    def _fetch(self, start, end):
        return self._range_request('bytes=%d-%d' % (start, end))

    def _fetch_suffix(self, n):
        return self._range_request('bytes=-%d' % n)

    def _range_request(self, range_header):
        req = urllib.request.Request(self._url, headers={
            'Range': range_header,
            'User-Agent': 'storetle-remote/0.2.1',
        })
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            if resp.status not in (200, 206):
                raise IOError('HTTP %d for %s' % (resp.status, self._url))
            if resp.status == 200 and range_header != 'bytes=0-':
                raise IOError(
                    'Server ignored Range request — remote access needs a '
                    'server that supports HTTP Range (got full response)')
            data = resp.read()
        self.bytes_fetched += len(data)
        return data

    # -- document access ----------------------------------------------------

    def _load_chunk(self, ci):
        if self._chunk_cache[0] == ci:
            return self._chunk_cache[1]
        off, expect_dc, _ = self._index[ci]
        raw = self._fetch(off, self._chunk_ends[ci] - 1)
        dc, _orig, comp_size = struct.unpack_from('>HII', raw, 0)
        if dc != expect_dc:
            raise ValueError('Chunk %d header disagrees with index' % ci)
        sizes = struct.unpack_from('>%dI' % dc, raw, 10)
        blob = _decompress(raw[10 + dc * 4: 10 + dc * 4 + comp_size], self._dict)
        docs, pos = [], 0
        for s in sizes:
            docs.append(blob[pos:pos + s])
            pos += s
        self._chunk_cache = (ci, docs)
        return docs

    def _locate(self, idx):
        lo, hi = 0, len(self._index) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cum[mid + 1] <= idx:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def __len__(self):
        return self.doc_count

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return [self[i] for i in range(*idx.indices(self.doc_count))]
        if idx < 0:
            idx += self.doc_count
        if not 0 <= idx < self.doc_count:
            raise IndexError('doc %d out of range (%d docs)' % (idx, self.doc_count))
        ci = self._locate(idx)
        docs = self._load_chunk(ci)
        return _decode_doc(docs[idx - self._cum[ci]])

    def __iter__(self):
        for ci in range(len(self._index)):
            for raw in self._load_chunk(ci):
                yield _decode_doc(raw)

    def info(self):
        comp = self._chunk_ends[-1] - self._index[0][0] if self._index else 0
        return {
            'docs': self.doc_count,
            'chunks': len(self._index),
            'original_bytes': sum(orig for _, _, orig in self._index),
            'compressed_bytes': comp,
            'ratio_pct': round(100 * (1 - comp / max(1, sum(o for _, _, o in self._index))), 2),
        }

    def close(self):
        self._chunk_cache = (None, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
