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


def cmd_unpack(args):
    text = '--text' in args
    args = [a for a in args if a != '--text']
    if len(args) < 2:
        print('Usage: storetle unpack <file-or-url> <output_folder> [--text]')
        sys.exit(1)
    src = args[0]
    dst = Path(args[1])
    dst.mkdir(parents=True, exist_ok=True)

    ext = 'txt' if text else 'html'
    with _open_reader(src) as r:
        print(f'Extracting {r.doc_count} documents to {dst}/ as .{ext}')
        docs = r.iter_text() if text else iter(r)
        for i, doc in enumerate(docs):
            out = dst / f'doc_{i:06d}.{ext}'
            out.write_bytes(doc)
            if (i + 1) % 100 == 0:
                print(f'  {i+1}/{r.doc_count}...')
    print(f'Done: {r.doc_count} files written to {dst}/')


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
    args = [a for a in args if a != '--text']
    if len(args) < 2:
        print('Usage: storetle get <file|url|corpus> <index|title> [--text]\n'
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
            doc = r.get_text(idx) if text else r[idx]
            sys.stdout.buffer.write(doc)
            if text:
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
  unpack    <file-or-url> <out> [--text] Extract → HTML files (or clean .txt)
  info      <file-or-url>               Show file statistics
  get       <file|url|corpus> <ref>    Extract one doc by index or title — remote
                                       reads fetch only the containing ~2MB chunk.
                                       fetches only the containing ~2MB chunk.
                                       Add --text for tag-stripped plain text
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
