# The `.storetle` Format Specification

Version 2 (`STREAM_VERSION = 2`). This document specifies the container and
document encoding precisely enough to write an independent implementation.
The reference implementations are `stream.py` (Python, read/write) and
`rust/` (Rust, read-only).

All multi-byte integers are **big-endian**. Notation: `u8`, `u16`, `u32`,
`u64` are unsigned integers of that width.

## 1. Container layout

A `.storetle` file is: a header, a sequence of chunks, an index, and a
16-byte footer. The index and footer live at the *end* of the file so the
format supports streaming writes.

```
┌────────────┐
│ Header     │  magic "STRL" (4) ‖ version u8 ‖ dict_size u32 ‖ dict bytes
├────────────┤
│ Chunk 0    │
│ Chunk 1    │  (see §2)
│ …          │
├────────────┤
│ Index      │  chunk_count × { file_offset u64 ‖ doc_count u16 ‖ orig_total u32 }
├────────────┤
│ Footer     │  chunk_count u64 ‖ index_offset u64     ← last 16 bytes of file
└────────────┘
```

- `magic` — ASCII `STRL`.
- `version` — currently `2`.
- `dict_size` — length of the zstd dictionary embedded in the header.
  Usually `0`: by default the dictionary is a **codec parameter**, shipped
  with the implementation (`cube_dict_v10.bin`, 1 MB), not stored per file.
  Writers may embed it (`embed_dict=True`) to make files self-contained.
- `index_offset` — absolute file offset of the index.
- Each index entry's `file_offset` is the absolute offset of that chunk.

**Reading procedure:** seek to EOF−16, read footer, seek to `index_offset`,
read `chunk_count` entries of 14 bytes each. Random access to document *i*
means scanning the (in-memory) index for the containing chunk, decompressing
only that chunk.

## 2. Chunk layout

Documents are batched into chunks of at most **256 documents** or **2 MiB**
of encoded bytes, whichever limit is reached first. Batching is what
preserves cross-document redundancy (shared templates, navigation,
boilerplate) while keeping random-access reads cheap.

```
doc_count  u16
orig_total u32                      total original (pre-encoding) bytes
comp_size  u32                      length of the compressed blob
sizes      doc_count × u32          length of each encoded document
blob       comp_size bytes          zstd-compressed concatenation of the
                                    encoded documents, in order
```

The blob is compressed with **zstd level 22** using the dictionary from §1.
(If zstd is unavailable the reference writer falls back to brotli q11; a
reader can distinguish by attempting zstd first — zstd frames begin with
magic `0x28 B5 2F FD`.) After decompression, document *k* occupies bytes
`[Σ sizes[0..k), Σ sizes[0..k])` of the concatenation.

## 3. Document encoding (NodeOp v2)

Each document is HTML re-encoded as two byte streams:

```
ss_size u32 ‖ struct_stream (ss_size bytes) ‖ content_stream (rest)
```

The **struct stream** carries document structure as compact opcodes; the
**content stream** carries text and out-of-vocabulary names. Splitting them
lets the entropy coder model each independently.

### 3.1 Vocabulary

The codec ships a fixed shared vocabulary (`vocab.py`):

- 130 tag IDs (`ID_TO_TAG`)
- 163 attribute-name IDs (`ID_TO_ATTR`)
- 1,394 shared strings (`SHARED_STRINGS`) — common attribute values,
  class tokens, URL fragments, etc.
- `UNKNOWN_ID = 0xFE` — sentinel meaning "name not in vocabulary; read it
  from the content stream."

### 3.2 Struct stream opcodes

| opcode | name | followed by |
|---|---|---|
| `0x01` | T_OPEN | tag_id u8 ‖ attr_count u8 ‖ attr_count × attr_id u8 |
| `0x02` | T_CLOSE | (nothing — decoder pops its open-tag stack) |
| `0x03` | T_TEXT | (payload in content stream; entity-escaped on decode) |
| `0x04` | T_DOCTYPE | (payload in content stream) |
| `0x05` | T_COMMENT | (payload in content stream) |
| `0x06` | T_SELFCLOSE | same operands as T_OPEN |
| `0x07` | T_RAWTEXT | (payload in content stream; emitted verbatim — `<script>`/`<style>` bodies) |

When `tag_id` or an `attr_id` equals `0xFE` (UNKNOWN_ID), the actual name is
the next string read from the content stream. Each attribute also consumes
one string read from the content stream for its **value**, in order.

### 3.3 Content stream string encoding

A "string read" decodes as follows, based on the first byte `b`:

| first byte | meaning |
|---|---|
| `0x00`–`0xFB` | shared string ID `b` (1 byte total) |
| `0xFC` | class-token list: `count u8`, then `count` recursive string reads, joined with single spaces. Used for `class` attribute values — each whitespace-separated token gets its own vocabulary lookup. |
| `0xFD` | inline string: `len u8` if ≤ 254, else `0xFF` followed by `len u32`; then `len` UTF-8 bytes |
| `0xFE` | None — boolean attribute (no value) |
| `0xFF` | `hi u8 ‖ lo u8` → shared string ID `(hi<<8)\|lo` (IDs 252–1393, 3 bytes total) |

### 3.4 Reconstruction semantics

Decoding is **structural, not byte-exact**: the decoder re-serializes the
node sequence as newline-separated, two-space-indented HTML. All tags,
attributes, attribute values, text, comments, doctype, and raw
script/style content are preserved; inter-tag whitespace and the original
source formatting are not. T_TEXT payloads are entity-escaped
(`& < >`) on output; attribute values escape `& " <`; T_RAWTEXT is emitted
verbatim.

Input bytes are decoded as UTF-8 (`errors='replace'`) before encoding, so
the round trip is defined over the UTF-8 interpretation of the source.

## 4. Versioning

- Container `version` byte gates the document encoding (`2` = NodeOp v2,
  class-token lists enabled).
- There is an older single-document format (`CUBE` magic, `decoder.py`)
  that predates the streaming container; it is legacy and not part of this
  spec.

## 5. Design rationale (informative)

- **Index-at-end + chunking** → streaming writes, O(1) random access reads
  (decompress ≤ 2 MiB per lookup), and HTTP range-request access against
  dumb object storage: fetch footer, fetch index, fetch one chunk.
- **Dictionary as codec parameter** → on million-document corpora, shipping
  the 1 MB dictionary once with the codec instead of per-file is strictly
  better; `embed_dict` exists for self-contained single files.
- **Struct/content split + tiny IDs** → the struct stream is mostly
  single-byte opcodes and IDs, which zstd models extremely well; measured
  results vs per-record-gzip WARC are ≈ 46% smaller on mixed pages (see
  README benchmarks).
