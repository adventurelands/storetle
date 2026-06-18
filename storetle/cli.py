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
    # (1a) explicit local interpreter bridge, if configured
    if _os.environ.get('STORETLE_VERIFIED_PYTHON'):
        try:
            from .verified_bridge import VerifiedBridge
            if _verified_bridge is None:
                _verified_bridge = VerifiedBridge()
            return _verified_bridge.extract(html_bytes)
        except Exception:
            pass
    # (1b) a locally-installed verified wheel, if present and loadable
    try:
        from storetle_verified import html_to_plaintext
        return html_to_plaintext(html_bytes).encode('utf-8')
    except Exception:
        pass
    # (2) the storetle API's verified pipeline (runs the Lean-proved extractor
    #     server-side). This is what makes --verified work from a plain
    #     `pip install storetle`, with no native libraries needed locally.
    try:
        from . import receipt as _r
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            _r.DEFAULT_API.rstrip('/') + '/verified_extract',
            data=html_bytes, method='POST')
        req.add_header('User-Agent', _r._UA)
        req.add_header('Content-Type', 'application/octet-stream')
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read().decode())['text'].encode('utf-8')
    except Exception:
        pass
    # (3) last resort: fast local extraction, never hard-error
    import re
    print('[storetle] verified pipeline unavailable here; used fast extraction',
          file=sys.stderr)
    txt = re.sub(rb'<[^>]+>', b' ', html_bytes)
    return re.sub(rb'\s+', b' ', txt).strip()


def _resolve_sources(src):
    """A local file or URL → [itself]; a corpus name → all its shard URLs."""
    if _is_url(src) or Path(src).is_file():
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
    if not _is_url(src) and not Path(src).is_file():
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


def cmd_search(args):
    if len(args) < 2:
        print('Usage: storetle search <corpus> <part-of-title>')
        sys.exit(1)
    from .registry import search_titles
    try:
        hits = search_titles(args[0], ' '.join(args[1:]))
    except KeyError as e:
        print(f'Error: {e}')
        sys.exit(1)
    if not hits:
        print('No matches.')
        sys.exit(1)
    for name, shard, idx in hits:
        print(f'  {idx:>9d}  (shard {shard})  {name}')
    print(f"\n  Fetch one:  storetle get {args[0]} <index> --text")


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


def cmd_stream(args):
    """Stream a whole corpus to stdout for training, optionally with a
    Bitcoin-anchored receipt of exactly the bytes you were served.

    Usage:
      storetle stream <corpus> [--text|--verified] [--limit N]
                               [--receipt] [--receipt-out FILE]
                               [--api URL] [--key KEY]

    Pipe it straight into a trainer:
      storetle stream uspto --text --verified --receipt | python train.py

    --text      tag-stripped plain text (fast, local)
    --verified  Lean-proved extraction via the verified pipeline
    --receipt   commit the streamed docs to a Merkle root, anchor it in
                Bitcoin via the storetle API, and save the receipt locally
    """
    import hashlib
    text = '--text' in args
    verified = '--verified' in args
    receipt = '--receipt' in args
    limit = None
    api_base = None
    api_key = None
    receipt_out = None
    pos = []
    i = 0
    flags = {'--text', '--verified', '--receipt'}
    while i < len(args):
        a = args[i]
        if a in flags:
            i += 1
        elif a == '--limit' and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2
        elif a == '--api' and i + 1 < len(args):
            api_base = args[i + 1]; i += 2
        elif a == '--key' and i + 1 < len(args):
            api_key = args[i + 1]; i += 2
        elif a == '--receipt-out' and i + 1 < len(args):
            receipt_out = args[i + 1]; i += 2
        else:
            pos.append(a); i += 1

    if not pos:
        print('Usage: storetle stream <corpus> [--text|--verified] [--limit N] '
              '[--receipt] [--api URL] [--key KEY]')
        sys.exit(1)
    corpus = pos[0]

    if receipt:
        # Option A: stream THROUGH the API, which hashes each doc as it serves,
        # so the receipt commits to exactly what we served this session (any
        # stop point). You pay 2x bandwidth only because you asked for a receipt.
        from . import receipt as _rcpt
        from .text import decode_text
        out_path = receipt_out or f'{corpus}.receipt.zip'
        got_hashes = []
        emitted = 0
        commit = None
        try:
            for item in _rcpt.iter_session_docs(corpus, api_base=api_base,
                                                api_key=api_key, limit=limit):
                if isinstance(item, tuple) and item and item[0] == '__commit__':
                    commit = item[1]
                    break
                raw = item
                got_hashes.append(hashlib.sha256(raw).hexdigest())
                if verified:
                    doc = _verified_extract(raw)
                elif text:
                    doc = decode_text(raw)
                else:
                    doc = raw
                if isinstance(doc, str):
                    doc = doc.encode()
                sys.stdout.buffer.write(doc)
                sys.stdout.buffer.write(b'\n')
                emitted += 1
            sys.stdout.buffer.flush()
        except Exception as e:
            print(f'[storetle] receipt stream failed: {e}', file=sys.stderr)
            sys.exit(2)
        print(f'[storetle] streamed {emitted} docs from "{corpus}" (with receipt)',
              file=sys.stderr)
        if not commit:
            print('[storetle] no commitment returned by server', file=sys.stderr)
            sys.exit(2)
        derived = _rcpt.merkle_root(got_hashes)
        match = derived == commit.get('merkle_root_hex')
        stream_meta = {'corpus': corpus, 'streamed_docs': emitted,
                       'streamed_root_hex': derived, 'locally_verified': match,
                       'created': commit.get('ts')}
        _rcpt.write_bundle(out_path, commit, stream_meta)
        if match:
            print("[storetle] VERIFIED: received bytes match storetle's signed, "
                  "Bitcoin-anchored session root", file=sys.stderr)
        else:
            print(f'[storetle] WARNING: local root {derived[:16]}... != server '
                  f'{str(commit.get("merkle_root_hex"))[:16]}...', file=sys.stderr)
        print(f'[storetle] receipt bundle: {out_path}', file=sys.stderr)
        print(f'[storetle] verify it:      storetle verify-receipt {out_path}',
              file=sys.stderr)
        return

    # --- no receipt: stream directly from R2 (cheap, no server in the path) ---
    from .registry import resolve, list_corpora
    try:
        info = list_corpora().get(corpus, {})
    except Exception:
        info = {}
    total = info.get('docs')
    n = limit if limit is not None else (total or 0)
    if not n:
        print(f'Error: cannot determine doc count for "{corpus}"; pass --limit N',
              file=sys.stderr)
        sys.exit(1)
    readers = {}
    emitted = 0
    try:
        for gidx in range(n):
            try:
                src, ref = resolve(corpus, gidx)
            except (KeyError, IndexError):
                break
            r = readers.get(src)
            if r is None:
                r = _open_reader(src).__enter__(); readers[src] = r
            ridx = int(ref)
            if verified:
                doc = _verified_extract(r[ridx])
            elif text:
                doc = r.get_text(ridx)
            else:
                doc = r[ridx]
            if isinstance(doc, str):
                doc = doc.encode()
            sys.stdout.buffer.write(doc)
            sys.stdout.buffer.write(b'\n')
            emitted += 1
    finally:
        for r in readers.values():
            try:
                r.__exit__(None, None, None)
            except Exception:
                pass
    sys.stdout.buffer.flush()
    print(f'[storetle] streamed {emitted} docs from "{corpus}"', file=sys.stderr)


def cmd_verify_receipt(args):
    """Verify a storetle receipt bundle (.zip): storetle's signature over the
    corpus root, and the Bitcoin (OpenTimestamps) anchor.

    Usage: storetle verify-receipt <receipt.zip>
    """
    if not args:
        print('Usage: storetle verify-receipt <receipt.zip>')
        sys.exit(1)
    from . import receipt as _rcpt
    try:
        signed, results = _rcpt.verify_bundle(args[0])
    except Exception as e:
        print(f'Error: cannot read receipt bundle: {e}')
        sys.exit(1)
    print(f'corpus:        {signed.get("corpus")}')
    print(f'documents:     {signed.get("doc_count")}')
    print(f'merkle root:   {signed.get("merkle_root_hex")}')
    print(f'streamed at:   {signed.get("ts")}')
    print()
    sig = results.get('signature')
    print(f'storetle signature:   {sig}')
    if 'ots_digest_matches_root' in results:
        print(f'.ots binds to root:   {results["ots_digest_matches_root"]}')
    print(f'bitcoin anchor:       {results.get("bitcoin")}')
    print()
    bitcoin = str(results.get('bitcoin', ''))
    ok = (sig == 'VALID') and results.get('ots_digest_matches_root', True)
    if ok and bitcoin.startswith('CONFIRMED'):
        print('RESULT: PASS — storetle-signed and Bitcoin-confirmed.')
    elif ok:
        print('RESULT: PASS — storetle-signed (Bitcoin anchor pending confirmation).')
    else:
        print('RESULT: FAIL — signature or anchor did not verify.')
        sys.exit(1)


COMMANDS = {
    'bench':         cmd_bench,
    'stream':        cmd_stream,
    'verify-receipt': cmd_verify_receipt,
    'corpora':     cmd_corpora,
    'search':      cmd_search,
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
  search    <corpus> <query>           Find documents by title substring
  bench     <folder>                   Benchmark your HTML data vs gzip WARC
  pack      <folder> <output>          Compress a folder → .storetle file
  unpack    <src> <out> [--text|--verified]  Extract → HTML, clean .txt, or
                                       formally verified plaintext
  info      <file-or-url>               Show file statistics
  get       <file|url|corpus> <ref>    Extract one doc by index or title — remote
                                       reads fetch only the containing ~2MB chunk.
                                       --text: tag-stripped plain text
                                       --verified: Lean-proved extraction
  stream    <corpus> [options]         Stream a whole corpus to stdout for training
                                       --text / --verified  extraction mode
                                       --limit N            stop after N docs
                                       --receipt            Bitcoin-anchored receipt
                                                            of exactly what you streamed
                                                            (writes <corpus>.receipt.zip)
                                       --api URL / --key K  verified API + key
  verify-receipt <receipt.zip>         Verify a receipt: storetle's signature +
                                       the Bitcoin anchor. (pip install 'storetle[verify]')
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
  storetle stream    uspto --text --verified --receipt | python train.py
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
