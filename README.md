# Storetle

## In plain English

Training an AI takes enormous piles of downloaded web pages, and those piles eat
up a huge amount of disk. Storetle squeezes them down much smaller than the usual
method (about 46% smaller than the standard gzipped web-archive format) — but
unlike a normal zip file, you can still reach in and grab any single page
instantly without unpacking the whole thing. Think of it as a vacuum-sealed
filing cabinet: way less space, but every folder is still right there when you
need it. A companion math proof (in the `storetle-verified` repo) double-checks
that the step which strips out junk like scripts and styling can never
accidentally leave that junk in the cleaned text.

---

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

**What `storetle bench <folder>` actually reproduces:** the gzip-WARC,
gzip-per-file, and storetle rows on **your own data** — that is the
**−46% vs gzip WARC** headline below, and it is fully reproducible from
the shipped CLI. The zstd and tar rows are **measured manually with the
system `zstd`/`tar` tools** (commands below); `storetle bench` does not
emit them. They are included for context, not as tool output.

**1. Among formats with random access** (you can extract one doc without
decompressing everything before it — this is how WARC is actually deployed):

| method | bytes | vs deployed standard | source |
|---|---|---|---|
| per-record gzip -9 (standard WARC) | 373,626 | — | `storetle bench` |
| per-record zstd -19 | 325,807 | −12.8% | manual (see below) |
| per-record zstd -19 + trained dict | 274,226 | −26.6% | manual (see below) |
| **storetle** | **200,598** | **−46.3%** | `storetle bench` |

**2. Against solid archives** (maximum compression, no random access) —
**all rows except storetle are measured manually**, not from `storetle bench`:

| method | bytes | source |
|---|---|---|
| tar + gzip -9 | 370,307 | manual |
| tar + zstd -19 | 220,512 | manual |
| tar + zstd -22 --long | 220,422 | manual |
| tar + zstd -22 + trained dict | 204,386 | manual |
| **storetle (keeps random access)** | **200,598** | `storetle bench` |

<details>
<summary>Exact commands for the manually-measured rows</summary>

```bash
# corpus/ = the 10 .html files; sizes are bytes of the resulting blobs.
# per-record (random-access) zstd:
for f in corpus/*.html; do zstd -19 -q -o "$f.zst" "$f"; done   # sum sizes → 325,807
zstd --train corpus/*.html -o corpus.dict                        # trained dictionary
for f in corpus/*.html; do zstd -19 -D corpus.dict -q -o "$f.zd" "$f"; done  # → 274,226

# solid archives (no random access):
tar cf - corpus | gzip -9            | wc -c   # → 370,307
tar cf - corpus | zstd -19           | wc -c   # → 220,512
tar cf - corpus | zstd -22 --long    | wc -c   # → 220,422
tar cf - corpus | zstd -22 -D corpus.dict | wc -c   # → 204,386
```

`storetle bench corpus/` produces the gzip-WARC, gzip-per-file, and
storetle numbers directly. Extending the bench tool to also shell out to
`zstd`/`tar` so these rows are auto-generated is on the roadmap.
</details>

Storetle matches solid zstd-22 while remaining randomly accessible. The
margin comes from three things: HTML-aware encoding (tags/attributes become
1-byte IDs from a shared vocabulary, structure and text compressed as
separate streams), a 1 MB dictionary trained on the binary encoding, and
256-document chunks that capture cross-page template redundancy.

On larger corpora measured against gzip WARC: 28.4% smaller on 3,000 live
Common Crawl docs (348.6 MB), 27–82% on same-domain collections (191 pages,
20 domains) where template sharing is strongest. Round-trip verified on all
of the above — **structurally, not byte-exactly**: every tag, attribute,
text node, comment, and script/style body is recovered, but HTML is
re-serialized (whitespace/indentation differ). See
[Limitations](#limitations--read-these). (Note: `storetle bench`'s own
`roundtrip_ok` flag only checks that the document *count* matches; the
structural fidelity is validated separately by `stress_test.py`.) Stream it
yourself: `python3 bench_cc.py --docs 3000`.

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
storetle search copyright "brown eyed girl"       # find records by title
storetle get copyright "Beyond the sea" --text    # a 1995 catalog assignment
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
point of the format.

**US Copyright Office public records** (5,929,094 documents, snapshot
2026-01, public domain): every recordation (catalog assignments, transfers,
security interests — 15.3M source rows grouped into 658,596 documents) plus
4.5M musical-work and 760K sound-recording registrations, parsed from the
official data.copyright.gov bulk files with per-record validation
(~2K malformed records quarantined, stats in the manifest). Under
`https://data.davisbrief.com/copyright/`. More corpora (arXiv, PubMed
Central OA) coming.

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
(hundreds of theorems; the load-bearing ones prove script/style content
never reaches the output and that ≈-equivalent documents yield identical
plaintext). The headline guarantee is the no-script/no-style/no-comment
leakage result, not raw theorem count. (Note: determinism here is inherent
to pure functions, not an earned theorem — the verified repo's own README is
explicit about which lemmas are vacuous and that the extracted C is trusted,
not independently proven equal to the spec.) For corpora where provenance
matters more than speed.

```bash
storetle get wiki "Black hole" --verified
```

Honest notes: it's slower than `--text` (re-parses via the proved WHATWG
tokenizer), its whitespace conventions differ from the fast extractor, and
the wheel ships separately (native Lean libraries; not on PyPI — the flag
explains how to get it if missing).

## Streaming a corpus, with a Bitcoin-anchored receipt (v0.5.0)

Stream a whole corpus to stdout for training, and walk away with a receipt of
exactly which bytes you were served:

```bash
storetle stream uspto --text --verified --receipt | python train.py
```

`--receipt` writes a self-contained `uspto.receipt.zip`. Here is the honest
description of what it proves, and what it does not:

- With `--receipt`, the stream runs **through the storetle API**, which hashes
  every document as it serves it and accumulates a Merkle root over exactly the
  bytes it sent you in this session. On finish, storetle signs that root
  (Ed25519) and anchors it into Bitcoin via
  [OpenTimestamps](https://opentimestamps.org). The commitment covers precisely
  what you streamed, at any stop point, computed by us, not asserted by you.
- The wheel independently re-derives the root from the bytes **it received** and
  confirms it matches storetle's signed root. A mismatch fails loudly.
- Verify the bundle yourself with one command (no trust in us required):

  ```bash
  storetle verify-receipt uspto.receipt.zip
  #   storetle signature: VALID
  #   bitcoin anchor:     CONFIRMED in block N   (PENDING for the first ~hours)
  ```

  Or by hand: `ots verify -d <merkle_root_hex> commitment.ots` (the `.ots` is in
  the zip in its native form, so the standard OpenTimestamps tools work directly).
  The Bitcoin attestation lands within a few hours of streaming; before that the
  receipt is committed to the OTS calendars and shows `PENDING`.

What it proves: the corpus bytes you trained on were served by storetle and
committed to a root storetle signed and anchored in Bitcoin. What it does not
prove: that any particular model consumed them — the training loop is yours;
storetle is the verifiable data tap, not the trainer.

Verifying needs the verifier extras: `pip install 'storetle[verify]'`.

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
