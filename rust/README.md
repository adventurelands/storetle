# storetle (Rust reader)

A reader-only Rust implementation of the `.storetle` archive format
(container parsing, zstd-with-dictionary chunk decompression, NodeOp → HTML
reconstruction). Output is byte-for-byte identical to the Python reference
decoder (`stream.StreamReader`), enforced by a checked-in differential test.

Layout:

- `Cargo.toml` — workspace root **and** the `storetle` package (lib + `storetle-rs` bin)
- `storetle-wasm/` — wasm-bindgen wrapper crate (see `../web/`)
- `codegen_vocab.py` — generates `src/vocab.rs` from the canonical `../vocab.py`
- `tests/gen_fixtures.py` — one-time fixture generator (Python implementation)
- `tests/fixtures/` — checked-in fixtures + Python-decoded expected outputs

## Build

```sh
cargo build --release          # native (libzstd via the `zstd` crate)
```

Feature flags:

| feature | backend | use |
|---|---|---|
| `zstd-native` (default) | `zstd` crate (C libzstd) | native CLI/lib |
| `zstd-pure` | `ruzstd` (pure Rust) | wasm32 builds; also works natively |

`ruzstd` ≥ 0.8 supports ZDICT-format dictionaries (`Dictionary::decode_dict`,
selected by the frame's dictionary id), so both backends decode
`cube_dict_v10.bin`-compressed archives.

## Test

```sh
cargo test                                        # native backend
cargo test --no-default-features --features zstd-pure   # pure-Rust backend
```

The differential test (`tests/differential.rs`) reads only the checked-in
fixtures — it never invokes Python. The fixtures were generated once with
`python3.11 tests/gen_fixtures.py` against the Python implementation
(`StreamWriter`/`StreamReader`); the `expected/*.html` files are the Python
decoder's exact output. Asserts: doc counts match, multi-chunk random access
(incl. chunk boundaries 255/256), embedded-dict files, and byte-for-byte HTML
equality on every compared doc.

> Note: `tests/gen_fixtures.py` was written against the pre-package flat
> repo layout (`from stream import StreamWriter`); update its import if the
> Python code has since been packaged.

## CLI

The dictionary is a codec parameter and is normally **not** stored in the
file. `storetle-rs` resolves it in this order: `--dict <path>` →
`$STORETLE_DICT` → `cube_dict_v10.bin` next to the input file →
`./cube_dict_v10.bin`. Files written with `embed_dict=True` need none.

```sh
# doc count + per-chunk stats
storetle-rs ls crawl.storetle --dict cube_dict_v10.bin

# print document #42's HTML to stdout
storetle-rs get crawl.storetle 42 --dict cube_dict_v10.bin

# write every doc as outdir/doc_NNNNNN.html
storetle-rs unpack crawl.storetle outdir --dict cube_dict_v10.bin
```

## Library

```rust
let dict = std::fs::read("cube_dict_v10.bin")?;
let mut r = storetle::StoretleReader::open("crawl.storetle", Some(dict))?;
println!("{} docs in {} chunks", r.doc_count(), r.chunk_count());
let html = r.get(42)?;                  // random access: one chunk decompressed
for doc in r.iter_docs() { /* sequential, chunk-at-a-time */ }
```

## Regenerating the vocab tables

`src/vocab.rs` is generated — do not hand-edit:

```sh
python3.11 codegen_vocab.py
```
