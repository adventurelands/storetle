# storetle — HTML-aware streaming compression for large document collections
#
# Primary API:
#   StreamWriter   — append HTML documents to a .storetle file
#   StreamReader   — read/iterate/random-access a .storetle file
#   pack           — compress a folder of HTML files → .storetle
#   unpack         — decompress .storetle → folder of HTML files
#   benchmark      — compare storetle vs gzip on your own data

from .stream import StreamWriter, StreamReader
from .remote import RemoteReader
from .folder import pack, unpack

__version__ = '0.3.0'
__all__ = ['StreamWriter', 'StreamReader', 'RemoteReader', 'pack', 'unpack', 'benchmark']


def benchmark(folder, quiet=False):
    """Benchmark storetle vs gzip WARC on a folder of HTML files.

    Returns a dict with size comparisons. Prints a table unless quiet=True.

    Example:
        import storetle
        results = storetle.benchmark('my_crawl_data/')
    """
    import gzip, os, tempfile, time
    from pathlib import Path

    files = sorted(Path(folder).glob('**/*.html'))
    if not files:
        raise ValueError(f'No .html files found in {folder}')

    docs = [f.read_bytes() for f in files]
    total_html = sum(len(d) for d in docs)

    # gzip WARC (industry standard)
    warc_raw = b''.join(
        'WARC/1.0\r\nContent-Length: {}\r\n\r\n'.format(len(d)).encode()
        + d + b'\r\n\r\n'
        for d in docs
    )
    warc_gz = len(gzip.compress(warc_raw, compresslevel=9))

    # gzip per-file
    gz_pf = sum(len(gzip.compress(d, compresslevel=9)) for d in docs)

    # storetle
    with tempfile.NamedTemporaryFile(suffix='.storetle', delete=False) as tf:
        tmp = tf.name
    try:
        t0 = time.time()
        with StreamWriter(tmp) as w:
            for d in docs:
                w.append(d)
        write_time = time.time() - t0

        t1 = time.time()
        with StreamReader(tmp) as r:
            recovered = list(r)
        read_time = time.time() - t1

        cube_size = os.path.getsize(tmp)
    finally:
        os.unlink(tmp)

    rt_ok = len(recovered) == len(docs)

    result = {
        'files':          len(files),
        'original_bytes': total_html,
        'gzip_warc':      warc_gz,
        'gzip_per_file':  gz_pf,
        'storetle':     cube_size,
        'savings_vs_gzip_warc_pct': round((warc_gz - cube_size) / warc_gz * 100, 1),
        'write_kbps':     int(total_html / 1024 / max(write_time, 0.001)),
        'read_kbps':      int(total_html / 1024 / max(read_time, 0.001)),
        'roundtrip_ok':   rt_ok,
    }

    if not quiet:
        _print_benchmark(result)

    return result


def _print_benchmark(r):
    def fmt(n):
        if n < 1024: return f'{n}B'
        if n < 1048576: return f'{n/1024:.1f}KB'
        return f'{n/1048576:.2f}MB'

    def pct(a, b):
        return f'{100*(1-a/b):.1f}%'

    orig = r['original_bytes']
    print(f'\n  storetle benchmark — {r["files"]} files, {fmt(orig)} original\n')
    print(f'  {"Format":<28} {"Size":>10}  {"Savings":>8}')
    print(f'  {"─"*50}')
    print(f'  {"Original HTML":<28} {fmt(orig):>10}')
    print(f'  {"gzip per-file (current)":<28} {fmt(r["gzip_per_file"]):>10}  {pct(r["gzip_per_file"], orig):>8}')
    print(f'  {"gzip WARC (Common Crawl std)":<28} {fmt(r["gzip_warc"]):>10}  {pct(r["gzip_warc"], orig):>8}')
    print(f'  {"storetle":<28} {fmt(r["storetle"]):>10}  {pct(r["storetle"], orig):>8}')
    print()

    saved = r["gzip_warc"] - r["storetle"]
    sign  = '+' if saved > 0 else ''
    print(f'  vs gzip WARC:  {sign}{r["savings_vs_gzip_warc_pct"]}% smaller  ({fmt(abs(saved))} {"saved" if saved > 0 else "larger"})')
    print(f'  Write speed:   {r["write_kbps"]:,} KB/s')
    print(f'  Read speed:    {r["read_kbps"]:,} KB/s')
    print(f'  Round-trip:    {"✓ all documents verified" if r["roundtrip_ok"] else "✗ FAILED"}')
    print()
