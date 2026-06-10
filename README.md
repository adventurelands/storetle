# Storetle

**HTML-aware compression for document corpora — solid-archive ratios with random access.**

Storetle stores large collections of HTML (web crawls, academic corpora,
training datasets) in a format that is ~46% smaller than the per-record
gzip WARC files the web-archiving world ships today, while still letting
you pull any single document out of a multi-gigabyte archive without
decompressing the rest — locally, or straight off object storage.

```
pip: storetle (Python, read/write)  ·  rust/: storetle-rs (Rust, read)  ·  web/: read .storetle in the browser
```

## The honest benchmark

Two different questions, two tables. Corpus: 10 real pages (Wikipedia,
arXiv abstracts, PLOS articles), 1.75 MB raw HTML, measured June 2026.
Reproduce with `storetle bench <folder>`.

**1. Among formats with random access** (you can extract one doc without
decompressing everything before it — this is how WARC is actually deployed):

| method | bytes | vs deployed standard |
|---|---|---|
| per-record gzip -9 (standard WARC) | 373,626 | — |
| per-record zstd -19 | 325,807 | −12.8% |
| per-record zstd -19 + trained dict | 274,226 | −26.6% |
| **storetle** | **200,598** | **−46.3%** |

**2. Against solid archives** (maximum compression, no random access):

| method | bytes |
|---|---|
| tar + gzip -9 | 370,307 |
| tar + zstd -19 | 220,512 |
| tar + zstd -22 --long | 220,422 |
| tar + zstd -22 + trained dict | 204,386 |
| **storetle (keeps random access)** | **200,598** |

Storetle matches solid zstd-22 while remaining randomly accessible. The
margin comes from three things: HTML-aware encoding (tags/attributes become
1-byte IDs from a shared vocabulary, structure and text compressed as
separate streams), a 1 MB dictionary trained on the binary encoding, and
256-document chunks that capture cross-page template redundancy.

On larger corpora measured against gzip WARC: 28.4% smaller on 3,000 live
Common Crawl docs (348.6 MB), 27–82% on same-domain collections (191 pages,
20 domains) where template sharing is strongest. Round-trip verified on all
of the above. Stream it yourself: `python3 bench_cc.py --docs 3000`.

## Install

```bash
brew install zstd        # macOS   (Ubuntu: apt install libzstd-dev)
pip install storetle
```

No Python dependencies — stdlib plus system libzstd via ctypes (brotli
fallback if zstd is missing). `lxml` is optional but strongly recommended
for encoding speed.

## CLI

```bash
storetle pack      my_crawl/ archive.storetle     # folder of .html → archive
storetle unpack    archive.storetle out/          # archive → .html files
storetle info      archive.storetle               # stats
storetle get       archive.storetle 42            # one doc to stdout, O(1)
storetle bench     my_crawl/                      # benchmark on YOUR data
storetle from-warc CC-MAIN.warc.gz archive.storetle
storetle to-warc   archive.storetle out.warc.gz
storetle train     my_corpus/ --output my.bin     # domain-specific dictionary
```

## Hosted corpora — free

```bash
storetle corpora                                  # list what's available
storetle get wiki "Albert Einstein" --text        # one article, by name, ~2s
storetle get wiki-text "Black hole"               # from the clean-text edition
```

Corpus names resolve through a public registry
(`https://data.davisbrief.com/corpora.json`) — new corpora appear without a
package update. Title lookup fetches a small index once and caches it.

**Available now — Simple English Wikipedia, complete** (267,503 articles,
snapshot 2025-03-20, CC-BY-SA-4.0):

| edition | size | contents |
|---|---|---|
| `wiki` | 843 MB / 6 shards | full article HTML (10.06 GB raw) |
| `wiki-text` | 196 MB / 1 file | clean plain text, random access |
| `…jsonl.zst` | 168 MB | `{"title","url","text"}` per line, for ML pipelines |

All under `https://data.davisbrief.com/simplewiki/` with JSONL metadata
indexes and a SHA-256 manifest. The entire text of Simple English Wikipedia
in 196 MB, where any article is one ~2 MB range request away — that's the
point of the format. More corpora (arXiv, PubMed Central OA) coming.

## Plain text extraction (v0.2.2)

`--text` on `get`/`unpack` (and `get_text()`/`iter_text()` in the API)
extracts tag-stripped clean text **without re-parsing HTML** — the encoding
already separates structure from content, so text extraction is a walk over
the structure opcodes that keeps text nodes, drops script/style bodies, and
emits newlines at block boundaries. A 383 KB Wikipedia article becomes 39 KB
of readable text.

## Formally verified extraction (v0.4.0)

`--verified` on `get`/`unpack` routes plaintext extraction through
[storetle-verified](https://github.com/adventurelands) — a Lean 4 pipeline
whose tokenizer, tree builder, and extraction carry machine-checked proofs
(621 theorems: script/style content provably never reaches output,
extraction provably deterministic). For corpora where provenance matters
more than speed.

```bash
storetle get wiki "Black hole" --verified
```

Honest notes: it's slower than `--text` (re-parses via the proved WHATWG
tokenizer), its whitespace conventions differ from the fast extractor, and
the wheel ships separately (native Lean libraries; not on PyPI — the flag
explains how to get it if missing).

## Remote archives (v0.2.1)

`get`, `info`, and `unpack` accept URLs. Opening an archive costs a few KB
of Range requests; fetching a document downloads only its ~2MB chunk — no
server-side code, works against any Range-capable host (R2, S3, GitHub
Pages, nginx):

```bash
storetle info https://data.davisbrief.com/simplewiki/simplewiki-text-20250320.storetle
storetle get  wiki "Albert Einstein" --text
```

```python
from storetle import RemoteReader
with RemoteReader('https://host/corpus.storetle') as r:
    html = r[42]          # one ~2MB range request
```

## Python API

```python
import storetle

with storetle.StreamWriter('archive.storetle', workers=8) as w:
    for html in crawl:
        w.append(html)

with storetle.StreamReader('archive.storetle') as r:
    print(r.doc_count)
    doc   = r[42]          # random access: decompresses one ~2MB chunk
    batch = r[100:200]
    for doc in r:          # sequential
        ...
```

## Rust reader

A read-only Rust implementation lives in [`rust/`](rust/) — library plus a
`storetle-rs` CLI (`ls` / `get` / `unpack`), differentially tested
byte-for-byte against the Python decoder.

## In the browser

[`web/`](web/) has a zero-dependency demo page: the Rust reader compiled to
WebAssembly. Drop a `.storetle` file onto the page and browse its documents.

## How it works

1. **Parse** — HTML is tokenized to a node stream (lxml fast path, pure-Python fallback).
2. **Encode** — tags and attribute names become 1-byte IDs from a fixed
   vocabulary (130 tags, 163 attributes, 1,394 shared strings).
   `class="flex items-center gap-4"` is split into per-token vocabulary
   lookups. Structure and text go to separate streams.
3. **Chunk** — up to 256 docs / 2 MiB are concatenated, preserving
   cross-document redundancy.
4. **Compress** — zstd-22 with a 1 MB dictionary trained on the binary
   encoding (ships with the codec).
5. **Index** — a footer index maps documents to chunks, so readers seek
   instead of scanning. Works over HTTP range requests against plain
   object storage.

Full byte-level spec: [FORMAT.md](FORMAT.md).

## Limitations — read these

- **Structural, not byte-exact.** Reconstructed HTML preserves every tag,
  attribute, text node, comment, and script/style body, but is
  re-serialized (indentation and inter-tag whitespace differ). Fine for
  corpora and ML pipelines; **wrong for byte-exact archival** — if you need
  forensic fidelity, use WARC.
- **HTML only.** `from-warc` keeps HTML response records and skips
  everything else. A raw passthrough mode for JSON/text is on the roadmap.
- **Encoding speed** ~3.5 MB/s per core (Python). Parallel via
  `workers=N`. Decoding is zstd-bound and fast. A native encoder is on the
  roadmap.
- **Alpha.** Format version 2. Validated on 150k+ Common Crawl documents,
  but expect rough edges.

## License

MIT © 2026 Davis Brief
