#!/usr/bin/env python3
# bench_cc.py — Benchmark storetle on live Common Crawl data.
#
# Streams a WARC segment directly from Common Crawl HTTPS — nothing stored locally
# except the final storetle output (which is deleted after stats are printed).
#
# Usage:
#   python3 bench_cc.py                        # 2000 docs, auto-detect latest crawl
#   python3 bench_cc.py --docs 5000            # more docs, better sample
#   python3 bench_cc.py --crawl CC-MAIN-2024-51 --segment 3
#   python3 bench_cc.py --save out.storetle    # keep the output file

import gzip
import io
import os
import sys
import time
import struct
import tempfile
import argparse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from storetle.warc import _parse_records, _split_http_response


# ---------------------------------------------------------------------------
# Common Crawl index helpers
# ---------------------------------------------------------------------------

def get_latest_crawl():
    """Return the most recent CC crawl ID by fetching the collinfo index."""
    url = 'https://index.commoncrawl.org/collinfo.json'
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            import json
            data = json.loads(resp.read())
        # Returns list of {id, name, timegate, ...}; first entry is most recent
        crawl_id = data[0]['id']
        print('Latest crawl: %s' % crawl_id)
        return crawl_id
    except Exception as e:
        fallback = 'CC-MAIN-2024-51'
        print('Could not fetch crawl list (%s), using %s' % (e, fallback))
        return fallback


def get_segment_url(crawl_id, segment_index=0):
    """Fetch the WARC paths list and return the URL for one segment."""
    paths_url = 'https://data.commoncrawl.org/crawl-data/%s/warc.paths.gz' % crawl_id
    print('Fetching segment list...')
    try:
        with urllib.request.urlopen(paths_url, timeout=30) as resp:
            raw = resp.read()
    except Exception as e:
        raise RuntimeError('Could not fetch %s: %s' % (paths_url, e))

    with gzip.open(io.BytesIO(raw)) as gz:
        paths = gz.read().decode().strip().splitlines()

    # Clamp index
    segment_index = segment_index % len(paths)
    path = paths[segment_index]
    url  = 'https://data.commoncrawl.org/' + path
    print('Segment %d of %d: %s' % (segment_index, len(paths), path))
    return url


# ---------------------------------------------------------------------------
# Streaming reader wrapper (counts compressed bytes received)
# ---------------------------------------------------------------------------

class _CountingRaw(io.RawIOBase):
    """Wraps a urllib response, counts raw bytes consumed."""
    def __init__(self, resp):
        self._resp  = resp
        self.total  = 0

    def readinto(self, b):
        chunk = self._resp.read(len(b))
        if not chunk:
            return 0
        n = len(chunk)
        b[:n] = chunk
        self.total += n
        return n

    def readable(self):
        return True


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------

def run_benchmark(url, max_docs=2000, save_path=None):
    from storetle.stream import StreamWriter, StreamReader

    print('\nStreaming Common Crawl WARC — up to %d HTML docs\n' % max_docs)

    tmp_path = save_path or tempfile.mktemp(suffix='.storetle')
    docs       = []          # collected HTML bytes for fair gzip comparison
    skipped    = 0
    t_start    = time.time()

    req  = urllib.request.Request(url, headers={'User-Agent': 'storetle-bench/0.1'})

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            counter    = _CountingRaw(resp)
            buf        = io.BufferedReader(counter, buffer_size=262144)
            # gzip.GzipFile handles concatenated per-record gzip streams transparently
            gz         = gzip.GzipFile(fileobj=buf)
            gz_buf     = io.BufferedReader(gz, buffer_size=262144)

            with StreamWriter(tmp_path) as writer:
                for warc_headers, payload in _parse_records(gz_buf):
                    if warc_headers.get('warc-type', '').lower() != 'response':
                        continue

                    http_headers, body = _split_http_response(payload)
                    ct = http_headers.get('content-type', '')

                    if not body or 'text/html' not in ct:
                        skipped += 1
                        continue

                    docs.append(body)
                    writer.append(body)

                    n = len(docs)
                    if n % 200 == 0:
                        elapsed  = time.time() - t_start
                        dl_mb    = counter.total / 1048576
                        html_mb  = sum(len(d) for d in docs) / 1048576
                        print('  %4d docs  %6.1f MB downloaded  %5.1f MB HTML  %4.0f KB/s' % (
                            n, dl_mb, html_mb, html_mb * 1024 / max(elapsed, 0.01)))

                    if n >= max_docs:
                        break

    except Exception as e:
        if not docs:
            raise
        print('\n  (stream ended early: %s)' % e)

    elapsed       = time.time() - t_start
    bytes_dl      = counter.total          # compressed bytes pulled from network
    storetle_size = os.path.getsize(tmp_path)
    html_orig     = sum(len(d) for d in docs)
    n_docs        = len(docs)

    # Build gzip WARC from the same HTML docs for a fair, apples-to-apples comparison
    print('\n  Computing gzip WARC baseline on same %d docs...' % n_docs)
    warc_raw = b''.join(
        ('WARC/1.0\r\nContent-Length: %d\r\n\r\n' % len(d)).encode() + d + b'\r\n\r\n'
        for d in docs
    )
    gz_warc_size = len(gzip.compress(warc_raw, compresslevel=9))
    del warc_raw  # free memory

    # Read speed
    t_read = time.time()
    with StreamReader(tmp_path) as r:
        _ = sum(1 for _ in r)
    read_elapsed = time.time() - t_read

    def fmt(n):
        if n < 1048576: return '%.1f KB' % (n / 1024)
        return '%.2f MB' % (n / 1048576)

    saved    = gz_warc_size - storetle_size
    saved_pct = saved / gz_warc_size * 100

    print()
    print('=' * 58)
    print('  Common Crawl Live Benchmark')
    print('  Source: %s' % url.split('/')[-1])
    print('=' * 58)
    print('  Documents:          %s HTML  (%s skipped non-HTML)' % (
        '{:,}'.format(n_docs), '{:,}'.format(skipped)))
    print('  Downloaded:         %s  (compressed WARC from CC)' % fmt(bytes_dl))
    print()
    print('  %-28s %12s  %8s' % ('Format', 'Size', 'vs orig'))
    print('  ' + '-' * 52)
    print('  %-28s %12s' % ('Original HTML', fmt(html_orig)))
    print('  %-28s %12s  %8s' % (
        'gzip WARC (Common Crawl std)', fmt(gz_warc_size),
        '%.1f%%' % (100 * (1 - gz_warc_size / html_orig))))
    print('  %-28s %12s  %8s' % (
        'storetle', fmt(storetle_size),
        '%.1f%%' % (100 * (1 - storetle_size / html_orig))))
    print()
    sign = '+' if saved > 0 else ''
    print('  vs gzip WARC:  %s%.1f%%  (%s %s)' % (
        sign, saved_pct, fmt(abs(saved)),
        'smaller' if saved > 0 else 'larger'))
    print('  Encode speed:  %.0f KB/s  (%.1fs total)' % (
        html_orig / 1024 / max(elapsed, 0.01), elapsed))
    print('  Read speed:    %.0f KB/s' % (
        html_orig / 1024 / max(read_elapsed, 0.01)))
    print('=' * 58)
    print()

    if not save_path and os.path.exists(tmp_path):
        os.unlink(tmp_path)
    elif save_path:
        print('  Archive saved: %s' % save_path)

    return {
        'docs':             n_docs,
        'html_orig':        html_orig,
        'gz_warc_size':     gz_warc_size,
        'storetle_size':    storetle_size,
        'savings_pct':      round(saved_pct, 1),
        'encode_kbps':      int(html_orig / 1024 / max(elapsed, 0.01)),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Benchmark storetle on live Common Crawl data (no local storage)')
    p.add_argument('--crawl',   default=None,  help='CC crawl ID (default: auto-detect latest)')
    p.add_argument('--segment', default=0,     type=int, help='Segment index (default: 0)')
    p.add_argument('--docs',    default=2000,  type=int, help='Max HTML docs (default: 2000)')
    p.add_argument('--save',    default=None,  help='Save output .storetle to this path')
    args = p.parse_args()

    crawl_id = args.crawl or get_latest_crawl()
    url      = get_segment_url(crawl_id, args.segment)
    run_benchmark(url, max_docs=args.docs, save_path=args.save)
