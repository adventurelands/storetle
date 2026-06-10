#!/usr/bin/env python3
# bench_parallel.py — Measure encoding throughput vs worker count.
#
# Downloads one CC segment, then encodes the same docs repeatedly
# at 1, 2, 4, 8, N cores and prints MB/s for each.
#
# Usage:
#   python3.11 bench_parallel.py
#   python3.11 bench_parallel.py --docs 1000 --max-workers 8

import os, sys, time, tempfile, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def fetch_docs(n=500):
    """Pull n HTML docs from CC for benchmarking."""
    import gzip, io, urllib.request
    from storetle.warc import _parse_records, _split_http_response

    import json
    with urllib.request.urlopen('https://index.commoncrawl.org/collinfo.json', timeout=10) as r:
        crawl_id = json.loads(r.read())[0]['id']

    paths_url = 'https://data.commoncrawl.org/crawl-data/%s/warc.paths.gz' % crawl_id
    with urllib.request.urlopen(paths_url, timeout=30) as resp:
        with gzip.open(io.BytesIO(resp.read())) as gz:
            path = gz.read().decode().strip().splitlines()[0]

    url = 'https://data.commoncrawl.org/' + path
    print('Fetching %d docs from %s...' % (n, crawl_id))

    docs = []
    req  = urllib.request.Request(url, headers={'User-Agent': 'storetle-bench/0.1'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        import io as _io
        buf = _io.BufferedReader(resp, buffer_size=262144)
        gz  = gzip.GzipFile(fileobj=buf)
        gz_buf = _io.BufferedReader(gz, buffer_size=262144)
        for wh, payload in _parse_records(gz_buf):
            if wh.get('warc-type', '').lower() != 'response':
                continue
            hh, body = _split_http_response(payload)
            if body and 'text/html' in hh.get('content-type', ''):
                docs.append(body)
            if len(docs) >= n:
                break

    total_mb = sum(len(d) for d in docs) / 1048576
    print('Got %d docs, %.1f MB HTML\n' % (len(docs), total_mb))
    return docs


def bench_workers(docs, n_workers):
    from storetle.stream import StreamWriter
    tmp = tempfile.mktemp(suffix='.storetle')
    total_bytes = sum(len(d) for d in docs)
    try:
        t0 = time.time()
        with StreamWriter(tmp, workers=n_workers) as w:
            for d in docs:
                w.append(d)
        elapsed = time.time() - t0
        return total_bytes / 1024 / elapsed  # KB/s
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--docs',        default=500, type=int)
    p.add_argument('--max-workers', default=None, type=int)
    args = p.parse_args()

    cpu_count   = os.cpu_count() or 4
    max_workers = args.max_workers or min(cpu_count, 16)

    # Worker counts to test: 1, 2, 4, 8, ... up to max
    counts = [1]
    w = 2
    while w <= max_workers:
        counts.append(w)
        w *= 2
    if counts[-1] != max_workers:
        counts.append(max_workers)

    docs = fetch_docs(args.docs)
    total_mb = sum(len(d) for d in docs) / 1048576

    print('%-10s  %10s  %12s  %10s' % ('Workers', 'MB/s', 'vs 1 core', 'efficiency'))
    print('-' * 48)

    baseline = None
    for n in counts:
        # Warm up on first run to exclude process spawn overhead from timing
        if n > 1:
            bench_workers(docs[:32], n)  # throwaway warmup

        kbps = bench_workers(docs, n)
        mbps = kbps / 1024

        if baseline is None:
            baseline = mbps
            speedup  = 1.0
            eff      = 1.0
        else:
            speedup = mbps / baseline
            eff     = speedup / n

        print('%-10d  %10.1f  %12.1fx  %9.0f%%' % (n, mbps, speedup, eff * 100))

    print()
    print('System: %d logical cores' % cpu_count)
    print()

    # Extrapolate to 150 TB (Common Crawl scale)
    best_mbps = bench_workers(docs, min(max_workers, cpu_count))
    tb = 150
    hours = (tb * 1024 * 1024) / best_mbps / 3600
    print('At %.0f MB/s (%d workers): 150 TB Common Crawl = %.0f hours on one machine' % (
        best_mbps, min(max_workers, cpu_count), hours))
    print('At 100-node cluster (same workers each): %.1f hours' % (hours / 100))
