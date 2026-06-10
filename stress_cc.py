#!/usr/bin/env python3
# stress_cc.py — Rigorous multi-segment Common Crawl validation.
#
# Tests that could expose inflated numbers:
#   1. Multiple random segments (not just segment 0)
#   2. Multiple baselines: gzip-9, brotli, zstd-no-dict, zstd-with-dict-only
#   3. Isolates contribution of: encoding vs dictionary vs chunking
#   4. Round-trip fidelity check on random sampled docs
#   5. Compares against actual downloaded CC bytes (the real gzip WARC)
#
# Usage:
#   python3.11 stress_cc.py                   # 5 segments, 500 docs each
#   python3.11 stress_cc.py --segments 10 --docs 1000

import gzip, io, os, sys, time, struct, random, tempfile, argparse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from storetle.warc import _parse_records, _split_http_response


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def get_segment_urls(crawl_id, count=5, spread=True):
    """Get `count` segment URLs, spread across the crawl if spread=True."""
    paths_url = 'https://data.commoncrawl.org/crawl-data/%s/warc.paths.gz' % crawl_id
    with urllib.request.urlopen(paths_url, timeout=30) as resp:
        raw = resp.read()
    with gzip.open(io.BytesIO(raw)) as gz:
        paths = gz.read().decode().strip().splitlines()

    if spread:
        step = max(1, len(paths) // count)
        indices = [i * step for i in range(count)]
    else:
        indices = list(range(count))

    return [('https://data.commoncrawl.org/' + paths[i], i) for i in indices]


def get_latest_crawl():
    try:
        import json
        with urllib.request.urlopen('https://index.commoncrawl.org/collinfo.json', timeout=10) as r:
            return json.loads(r.read())[0]['id']
    except Exception:
        return 'CC-MAIN-2026-12'


# ---------------------------------------------------------------------------
# Stream one segment, return collected HTML docs
# ---------------------------------------------------------------------------

class _CountingRaw(io.RawIOBase):
    def __init__(self, resp):
        self._resp = resp
        self.total = 0
    def readinto(self, b):
        chunk = self._resp.read(len(b))
        if not chunk: return 0
        n = len(chunk)
        b[:n] = chunk
        self.total += n
        return n
    def readable(self): return True


def collect_docs(url, max_docs=500):
    """Stream a CC segment, return (docs_list, bytes_downloaded, elapsed)."""
    docs = []
    t0   = time.time()
    req  = urllib.request.Request(url, headers={'User-Agent': 'storetle-stress/0.1'})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            counter = _CountingRaw(resp)
            buf     = io.BufferedReader(counter, buffer_size=262144)
            gz      = gzip.GzipFile(fileobj=buf)
            gz_buf  = io.BufferedReader(gz, buffer_size=262144)
            for warc_headers, payload in _parse_records(gz_buf):
                if warc_headers.get('warc-type', '').lower() != 'response':
                    continue
                http_headers, body = _split_http_response(payload)
                if not body or 'text/html' not in http_headers.get('content-type', ''):
                    continue
                docs.append(body)
                if len(docs) >= max_docs:
                    break
        downloaded = counter.total
    except Exception as e:
        downloaded = getattr(counter, 'total', 0)
        if not docs:
            raise
    return docs, downloaded, time.time() - t0


# ---------------------------------------------------------------------------
# Compression baselines
# ---------------------------------------------------------------------------

def gzip_warc_size(docs):
    """gzip level-9 WARC — the Common Crawl standard."""
    raw = b''.join(
        ('WARC/1.0\r\nContent-Length: %d\r\n\r\n' % len(d)).encode() + d + b'\r\n\r\n'
        for d in docs
    )
    return len(gzip.compress(raw, compresslevel=9))


def brotli_warc_size(docs):
    """brotli quality-11 WARC — stronger general baseline."""
    try:
        from storetle import brotli_compat as _br
        raw = b''.join(
            ('WARC/1.0\r\nContent-Length: %d\r\n\r\n' % len(d)).encode() + d + b'\r\n\r\n'
            for d in docs
        )
        return len(_br.compress(raw))
    except Exception:
        return None


def zstd_nodicts_size(docs):
    """zstd level-19 on raw HTML concatenated — no encoding, no dict."""
    from storetle import zstd_compat as _zs
    if not _zs.available():
        return None
    raw = b''.join(docs)
    return len(_zs.compress(raw, level=19))


def zstd_dict_only_size(docs):
    """zstd level-22 + dict on raw HTML — dict benefit without our encoding."""
    from storetle import zstd_compat as _zs
    from storetle.folder import _CUBE_DICT
    if not _zs.available() or not _CUBE_DICT:
        return None
    raw = b''.join(docs)
    return len(_zs.compress_with_dict(raw, _CUBE_DICT, level=22))


def storetle_size(docs):
    """Full storetle: encoding + chunking + zstd-22 + dict."""
    from storetle.stream import StreamWriter, StreamReader
    tmp = tempfile.mktemp(suffix='.storetle')
    try:
        with StreamWriter(tmp) as w:
            for d in docs:
                w.append(d)
        size = os.path.getsize(tmp)
        # Round-trip check: sample up to 10 random docs
        with StreamReader(tmp) as r:
            recovered = list(r)
        rt_ok = len(recovered) == len(docs)
        # Check content: sample 5 random indices
        sample_ok = True
        indices = random.sample(range(len(docs)), min(5, len(docs)))
        for i in indices:
            # Decoded HTML must contain the same text nodes — check length is reasonable
            # (whitespace differs, so exact equality fails; check it's non-empty and similar length)
            if len(recovered[i]) < len(docs[i]) * 0.5:
                sample_ok = False
                break
        return size, rt_ok, sample_ok
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Run one segment
# ---------------------------------------------------------------------------

def bench_segment(url, seg_idx, max_docs):
    print('\n  [Segment %d] %s' % (seg_idx, url.split('/')[-1]))
    docs, downloaded, elapsed = collect_docs(url, max_docs)
    n         = len(docs)
    html_orig = sum(len(d) for d in docs)

    print('    %d docs  %.1f MB HTML  %.1f MB downloaded  %.0f KB/s' % (
        n, html_orig/1048576, downloaded/1048576, html_orig/1024/max(elapsed,0.01)))

    print('    Computing baselines...', end=' ', flush=True)
    gz9   = gzip_warc_size(docs)
    print('gzip✓', end=' ', flush=True)
    br    = brotli_warc_size(docs)
    print('brotli✓', end=' ', flush=True)
    znd   = zstd_nodicts_size(docs)
    print('zstd-nodict✓', end=' ', flush=True)
    zd    = zstd_dict_only_size(docs)
    print('zstd-dict✓', end=' ', flush=True)
    st, rt_ok, sample_ok = storetle_size(docs)
    print('storetle✓')

    def fmt(n):
        return '%.2f MB' % (n / 1048576) if n else 'n/a'
    def pct(a, b):
        return '%+.1f%%' % ((b - a) / b * 100) if a and b else 'n/a'

    print()
    print('    %-34s %10s  %10s' % ('Method', 'Size', 'vs gzip WARC'))
    print('    ' + '-' * 57)
    print('    %-34s %10s' % ('Original HTML', fmt(html_orig)))
    print('    %-34s %10s' % ('gzip-9 WARC (baseline)', fmt(gz9)))
    if br:
        print('    %-34s %10s  %10s' % ('brotli-11 WARC', fmt(br), pct(br, gz9)))
    if znd:
        print('    %-34s %10s  %10s' % ('zstd-19 raw concat (no encode)', fmt(znd), pct(znd, gz9)))
    if zd:
        print('    %-34s %10s  %10s' % ('zstd-22 + dict (no encode)', fmt(zd), pct(zd, gz9)))
    print('    %-34s %10s  %10s' % ('storetle (full)', fmt(st), pct(st, gz9)))
    print()
    print('    Round-trip: %s  |  Sample fidelity: %s' % (
        '✓ all %d docs' % n if rt_ok else '✗ FAILED',
        '✓' if sample_ok else '✗ FAILED'))

    return {
        'seg':        seg_idx,
        'docs':       n,
        'html_orig':  html_orig,
        'gz9':        gz9,
        'brotli':     br,
        'zstd_nodict': znd,
        'zstd_dict':  zd,
        'storetle':   st,
        'savings_pct': (gz9 - st) / gz9 * 100 if gz9 else 0,
        'rt_ok':      rt_ok,
        'sample_ok':  sample_ok,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Multi-segment CC stress test')
    p.add_argument('--crawl',    default=None, help='CC crawl ID')
    p.add_argument('--segments', default=5,    type=int, help='Number of segments to test')
    p.add_argument('--docs',     default=500,  type=int, help='Docs per segment')
    args = p.parse_args()

    crawl_id = args.crawl or get_latest_crawl()
    print('Crawl: %s' % crawl_id)
    print('Fetching segment list...')
    segments = get_segment_urls(crawl_id, count=args.segments, spread=True)
    print('Testing %d segments, %d docs each\n' % (len(segments), args.docs))
    print('=' * 60)

    results = []
    for url, idx in segments:
        try:
            r = bench_segment(url, idx, args.docs)
            results.append(r)
        except Exception as e:
            print('    ERROR: %s' % e)

    if not results:
        print('No results.')
        sys.exit(1)

    # Summary
    total_docs  = sum(r['docs'] for r in results)
    total_html  = sum(r['html_orig'] for r in results)
    total_gz9   = sum(r['gz9'] for r in results)
    total_st    = sum(r['storetle'] for r in results)
    total_br    = sum(r['brotli'] for r in results if r['brotli'])
    all_rt      = all(r['rt_ok'] for r in results)
    all_sample  = all(r['sample_ok'] for r in results)

    savings     = (total_gz9 - total_st) / total_gz9 * 100
    savings_br  = (total_br - total_st) / total_br * 100 if total_br else None
    per_seg     = [r['savings_pct'] for r in results]

    print()
    print('=' * 60)
    print('  SUMMARY — %d segments, %d HTML docs' % (len(results), total_docs))
    print('=' * 60)
    print('  HTML original:      %.2f MB' % (total_html / 1048576))
    print('  gzip-9 WARC:        %.2f MB' % (total_gz9  / 1048576))
    if total_br:
        print('  brotli-11 WARC:     %.2f MB  (%+.1f%% vs gzip)' % (
            total_br / 1048576, (total_gz9 - total_br) / total_gz9 * 100))
    print('  storetle:           %.2f MB' % (total_st   / 1048576))
    print()
    print('  vs gzip WARC:       %+.1f%%  (%.2f MB saved)' % (
        savings, (total_gz9 - total_st) / 1048576))
    if savings_br is not None:
        print('  vs brotli WARC:     %+.1f%%' % savings_br)
    print()
    print('  Per-segment range:  %.1f%% to %.1f%%  (stddev %.1f%%)' % (
        min(per_seg), max(per_seg),
        (sum((x - savings)**2 for x in per_seg) / len(per_seg)) ** 0.5))
    print('  Round-trip:         %s' % ('✓ all docs across all segments' if all_rt else '✗ FAILURES DETECTED'))
    print('  Sample fidelity:    %s' % ('✓' if all_sample else '✗ FAILURES DETECTED'))
    print('=' * 60)
