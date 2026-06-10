#!/usr/bin/env python3
# cli.py — storetle command-line interface
#
# Commands:
#   storetle bench  <folder>                 — benchmark your data
#   storetle pack   <folder> <output>        — compress folder → .storetle
#   storetle unpack <input>  <output_folder> — decompress .storetle → files
#   storetle info   <file.storetle>        — show file stats
#   storetle get    <file.storetle> <idx>  — extract one document by index

import sys
from pathlib import Path



def cmd_bench(args):
    if not args:
        print('Usage: storetle bench <folder>')
        sys.exit(1)
    from . import benchmark
    try:
        benchmark(args[0])
    except ValueError as e:
        print(f'Error: {e}')
        sys.exit(1)


def cmd_pack(args):
    if len(args) < 2:
        print('Usage: storetle pack <folder> <output.storetle>')
        sys.exit(1)
    from .stream import StreamWriter
    folder = Path(args[0])
    output = args[1]
    files  = sorted(folder.glob('**/*.html'))
    if not files:
        print(f'No .html files found in {folder}')
        sys.exit(1)

    print(f'Packing {len(files)} files...')
    with StreamWriter(output) as w:
        for f in files:
            w.append(f.read_bytes())

    from .stream import StreamReader
    info = StreamReader.info(output)

    def fmt(n):
        if n < 1048576: return f'{n/1024:.1f}KB'
        return f'{n/1048576:.2f}MB'

    print(f'Done: {fmt(info["original_bytes"])} → {fmt(info["compressed_bytes"])} '
          f'({info["ratio_pct"]}% saved, {info["docs"]} docs, {info["chunks"]} chunks)')
    print(f'Output: {output}')


def _is_url(s):
    return s.startswith('http://') or s.startswith('https://')


def _open_reader(src):
    """Open a local path with StreamReader or a URL with RemoteReader."""
    if _is_url(src):
        from .remote import RemoteReader
        return RemoteReader(src)
    from .stream import StreamReader
    return StreamReader(src)


_verified_bridge = None


def _verified_extract(html_bytes):
    """Plaintext via the formally verified pipeline (storetle-verified wheel).

    Unlike --text (fast opcode walk in this package), this re-runs the
    Lean-proved WHATWG tokenizer + tree builder + extraction over the
    reconstructed HTML — slower, but machine-checked.

    If STORETLE_VERIFIED_PYTHON is set, extraction runs in that interpreter
    instead — for machines where this Python's CPU architecture doesn't
    match the verified wheel's native libraries.
    """
    global _verified_bridge
    import os as _os
    if _os.environ.get('STORETLE_VERIFIED_PYTHON'):
        from .verified_bridge import VerifiedBridge
        if _verified_bridge is None:
            _verified_bridge = VerifiedBridge()
        try:
            return _verified_bridge.extract(html_bytes)
        except (ValueError, RuntimeError) as e:
            print(f'Error: verified bridge failed: {e}', file=sys.stderr)
            sys.exit(1)

    try:
        from storetle_verified import html_to_plaintext
    except ImportError:
        print('Error: --verified requires the storetle-verified wheel '
              '(formally verified extraction pipeline).\n'
              'It is not on PyPI; build/install it from the storetle-verified '
              'repository, or omit --verified to use the fast built-in '
              'extractor (--text).', file=sys.stderr)
        sys.exit(1)
    try:
        # native Lean libraries load lazily on first call
        return html_to_plaintext(html_bytes).encode('utf-8')
    except OSError as e:
        print('Error: storetle-verified is installed but its native '
              'libraries failed to load.\n'
              'Most common cause: CPU architecture mismatch between this '
              'Python and the wheel (e.g. x86_64 Python with arm64 libs).\n'
              'Fix: set STORETLE_VERIFIED_PYTHON to an interpreter matching '
              'the libraries (plus STORETLE_VERIFIED_PYTHONPATH to the '
              'directory containing storetle_verified, if needed).\n'
              f'Loader said: {e}', file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f'Error: verified pipeline rejected input: {e}', file=sys.stderr)
        sys.exit(1)


def _resolve_sources(src):
    """A local file or URL → [itself]; a corpus name → all its shard URLs."""
    if _is_url(src) or Path(src).exists():
        return [src]
    from .registry import load_registry
    try:
        reg = load_registry()
    except Exception as e:
        print(f"Error: '{src}' is not a file or URL, and the corpus "
              f'registry could not be fetched ({e})', file=sys.stderr)
        sys.exit(1)
    if src in reg:
        e = reg[src]
        base = e['base'].rstrip('/')
        return [f'{base}/{s}' for s in e['shards']]
    print(f"Error: '{src}' is not a file, URL, or known corpus.\n"
          f'Known corpora: {", ".join(sorted(reg))}   (see: storetle corpora)',
          file=sys.stderr)
    sys.exit(1)


def cmd_unpack(args):
    text = '--text' in args
    verified = '--verified' in args
    args = [a for a in args if a not in ('--text', '--verified')]
    if len(args) < 2:
        print('Usage: storetle unpack <file|url|corpus> <output_folder> [--text|--verified]\n'
              '       Extracts EVERY document. For one document, use: storetle get')
        sys.exit(1)
    srcs = _resolve_sources(args[0])
    dst = Path(args[1])
    dst.mkdir(parents=True, exist_ok=True)

    ext = 'txt' if (text or verified) else 'html'
    label = 'verified plaintext' if verified else ext
    i = 0
    t0 = __import__('time').time()

    pool = None
    if not verified:
        # decode is CPU-bound pure Python — fan it across cores while the
        # reader prefetches the next chunk over the network
        from multiprocessing import Pool, cpu_count
        pool = Pool(min(8, max(1, cpu_count() - 1)))
        from .stream import _decode_doc
        from .text import decode_text
        decode_fn = decode_text if text else _decode_doc

    try:
        for sno, src in enumerate(srcs):
            with _open_reader(src) as r:
                if sno == 0:
                    scope = f' (shard 1/{len(srcs)})' if len(srcs) > 1 else ''
                    print(f'Extracting to {dst}/ as {label}{scope} — '
                          f'{r.doc_count} docs in first source')
                elif len(srcs) > 1:
                    print(f'  shard {sno+1}/{len(srcs)} ({r.doc_count} docs)...')
                if verified:
                    docs = (_verified_extract(d) for d in r)
                else:
                    docs = pool.imap(decode_fn, r.iter_raw(), chunksize=32)
                for doc in docs:
                    (dst / f'doc_{i:06d}.{ext}').write_bytes(doc)
                    i += 1
                    if i % 5000 == 0:
                        rate = i / max(1e-9, __import__('time').time() - t0)
                        print(f'  {i}  ({rate:.0f} docs/s)')
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()
    print(f'Done: {i} files written to {dst}/')


def cmd_info(args):
    if not args:
        print('Usage: storetle info <file-or-url>')
        sys.exit(1)

    def fmt(n):
        if n < 1048576: return f'{n/1024:.1f}KB'
        return f'{n/1048576:.2f}MB'

    if _is_url(args[0]):
        from .remote import RemoteReader
        with RemoteReader(args[0]) as r:
            info = r.info()
    else:
        from .stream import StreamReader
        info = StreamReader.info(args[0])
    print(f'\n  {args[0]}')
    print(f'  Documents:    {info["docs"]:,}')
    print(f'  Chunks:       {info["chunks"]:,}')
    print(f'  Original:     {fmt(info["original_bytes"])}')
    print(f'  Compressed:   {fmt(info["compressed_bytes"])}  ({info["ratio_pct"]}% saved)')
    print()


def cmd_get(args):
    text = '--text' in args
    verified = '--verified' in args
    args = [a for a in args if a not in ('--text', '--verified')]
    if len(args) < 2:
        print('Usage: storetle get <file|url|corpus> <index|title> [--text|--verified]\n'
              '       storetle get wiki "Albert Einstein" --text')
        sys.exit(1)

    src, ref = args[0], ' '.join(args[1:])
    if not _is_url(src) and not Path(src).exists():
        # treat as a named corpus from the public registry
        from .registry import resolve
        try:
            src, ref = resolve(src, ref)
        except (KeyError, IndexError) as e:
            print(f'Error: {e}')
            sys.exit(1)

    with _open_reader(src) as r:
        try:
            idx = int(ref)
            if verified:
                doc = _verified_extract(r[idx])
            elif text:
                doc = r.get_text(idx)
            else:
                doc = r[idx]
            sys.stdout.buffer.write(doc)
            if text or verified:
                sys.stdout.buffer.write(b'\n')
        except (IndexError, ValueError) as e:
            print(f'Error: {e}')
            sys.exit(1)


def cmd_corpora(args):
    from .registry import list_corpora
    print()
    for name, info in list_corpora().items():
        print(f'  {name:12s} {info.get("title","")}  '
              f'[{info.get("docs","?"):,} docs, {info.get("snapshot","")}, '
              f'{info.get("license","")}]')
    print('\n  Usage: storetle get <corpus> <title-or-index> [--text]')
    print()


def cmd_from_warc(args):
    if len(args) < 2:
        print('Usage: storetle from-warc <input.warc[.gz]> <output.storetle>')
        sys.exit(1)
    from .warc import from_warc
    try:
        from_warc(args[0], args[1], verbose=True)
    except ValueError as e:
        print('Error: %s' % e)
        sys.exit(1)
    except Exception as e:
        print('Error: %s' % e)
        sys.exit(1)


def cmd_to_warc(args):
    if len(args) < 2:
        print('Usage: storetle to-warc <input.storetle> <output.warc[.gz]>')
        sys.exit(1)
    from .warc import to_warc
    try:
        to_warc(args[0], args[1], verbose=True)
    except Exception as e:
        print('Error: %s' % e)
        sys.exit(1)


def cmd_train(args):
    """Train a custom zstd dictionary from a folder of HTML files.

    Usage: storetle train <folder> [--output dict.bin] [--size 1024]

    The trained dict can then be used with StreamWriter(path, dictionary=...).
    Default output: storetle_dict.bin in current directory.
    Default size: 1024KB (the same as the built-in dict).
    """
    if not args:
        print('Usage: storetle train <folder> [--output dict.bin] [--size 1024]')
        sys.exit(1)

    folder  = Path(args[0])
    outfile = 'storetle_dict.bin'
    size_kb = 1024

    i = 1
    while i < len(args):
        if args[i] == '--output' and i + 1 < len(args):
            outfile = args[i + 1]; i += 2
        elif args[i] == '--size' and i + 1 < len(args):
            try:
                size_kb = int(args[i + 1])
            except ValueError:
                print('Error: --size must be an integer (KB)')
                sys.exit(1)
            i += 2
        else:
            i += 1

    from . import zstd_compat as _zs
    if not _zs.available():
        print('Error: zstd not available. Install libzstd first.')
        sys.exit(1)

    from .stream import _encode_doc

    files = sorted(folder.glob('**/*.html'))
    if not files:
        print('Error: no .html files found in %s' % folder)
        sys.exit(1)

    print('Encoding %d HTML files for dictionary training...' % len(files))
    samples = []
    errors  = 0
    for f in files:
        try:
            raw = _encode_doc(f.read_bytes())
            samples.append(raw)
        except Exception:
            errors += 1

    if not samples:
        print('Error: failed to encode any files')
        sys.exit(1)

    total_kb = sum(len(s) for s in samples) // 1024
    print('Training %dKB dictionary on %d samples (%dKB total)...' % (
        size_kb, len(samples), total_kb))

    dict_bytes = _zs.train_dictionary(samples, dict_size=size_kb * 1024)

    Path(outfile).write_bytes(dict_bytes)
    actual_kb = len(dict_bytes) // 1024
    print('Done: %dKB dictionary saved to %s' % (actual_kb, outfile))
    if errors:
        print('  (%d files skipped due to encoding errors)' % errors)
    print()
    print('To use this dictionary:')
    print('  import storetle')
    print('  d = open("%s", "rb").read()' % outfile)
    print('  with storetle.StreamWriter("out.storetle", dictionary=d) as w:')
    print('      w.append(html)')
    print('  with storetle.StreamReader("out.storetle") as r:')
    print('      # reader auto-loads dict from disk; pass dictionary=d if custom')


def cmd_warc_encode(args):
    if len(args) < 2:
        print('Usage: storetle warc-encode <input.warc[.gz]> <output.warc[.gz]>')
        sys.exit(1)
    from .warc import warc_encode
    try:
        warc_encode(args[0], args[1], verbose=True)
    except Exception as e:
        print('Error: %s' % e)
        sys.exit(1)


def cmd_warc_decode(args):
    if len(args) < 2:
        print('Usage: storetle warc-decode <encoded.warc[.gz]> <output.warc[.gz]>')
        sys.exit(1)
    from .warc import warc_decode
    try:
        warc_decode(args[0], args[1], verbose=True)
    except Exception as e:
        print('Error: %s' % e)
        sys.exit(1)


COMMANDS = {
    'bench':       cmd_bench,
    'corpora':     cmd_corpora,
    'pack':        cmd_pack,
    'unpack':      cmd_unpack,
    'info':        cmd_info,
    'get':         cmd_get,
    'from-warc':   cmd_from_warc,
    'to-warc':     cmd_to_warc,
    'warc-encode': cmd_warc_encode,
    'warc-decode': cmd_warc_decode,
    'train':       cmd_train,
}

HELP = """storetle — HTML-aware compression for large document collections

Commands:
  corpora                              List free hosted corpora
  bench     <folder>                   Benchmark your HTML data vs gzip WARC
  pack      <folder> <output>          Compress a folder → .storetle file
  unpack    <src> <out> [--text|--verified]  Extract → HTML, clean .txt, or
                                       formally verified plaintext
  info      <file-or-url>               Show file statistics
  get       <file|url|corpus> <ref>    Extract one doc by index or title — remote
                                       reads fetch only the containing ~2MB chunk.
                                       --text: tag-stripped plain text
                                       --verified: Lean-proved extraction
  from-warc   <input.warc[.gz]> <out>  Convert WARC → .storetle
  to-warc     <input.storetle> <out>  Convert .storetle → WARC (or .warc.gz)
  warc-encode <input.warc[.gz]> <out> Encode HTML in-place → valid .warc.gz (smaller, standard format)
  warc-decode <encoded.warc[.gz]> <out> Decode back to standard HTML WARC
  train       <folder> [options]       Train a custom dictionary from HTML files
            --output  dict.bin           Output path (default: storetle_dict.bin)
            --size    1024               Dictionary size in KB (default: 1024)

Examples:
  storetle bench     my_crawl/
  storetle pack      my_crawl/  archive.storetle
  storetle info      archive.storetle
  storetle get       archive.storetle 0
  storetle from-warc CC-MAIN.warc.gz  archive.storetle
  storetle to-warc   archive.storetle output.warc.gz
  storetle train     my_corpus/ --output my_domain.bin --size 1024
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        print(HELP)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f'Unknown command: {cmd}\n')
        print(HELP)
        sys.exit(1)

    try:
        COMMANDS[cmd](sys.argv[2:])
    except BrokenPipeError:
        # downstream consumer (e.g. `| head`) closed the pipe — not an error
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == '__main__':
    main()
