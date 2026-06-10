//! Differential round-trip test against the Python reference implementation.
//!
//! The fixtures under tests/fixtures/ were produced by tests/gen_fixtures.py
//! (one-time, using the Python StreamWriter); the expected/*.html files are
//! the Python StreamReader's decoded output. This test asserts the Rust
//! reader produces byte-for-byte identical HTML. It never runs Python.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use storetle::StoretleReader;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn read_dict() -> Vec<u8> {
    std::fs::read(fixtures().join("cube_dict_v10.bin")).expect("dictionary fixture missing")
}

fn manifest() -> HashMap<String, String> {
    let text = std::fs::read_to_string(fixtures().join("manifest.txt")).expect("manifest");
    text.lines()
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect()
}

fn expected(prefix: &str, idx: u64) -> Vec<u8> {
    let p = fixtures().join(format!("expected/{prefix}_{idx:06}.html"));
    std::fs::read(&p).unwrap_or_else(|e| panic!("missing expected file {}: {e}", p.display()))
}

fn assert_doc_matches(prefix: &str, idx: u64, rust_html: &str) {
    let want = expected(prefix, idx);
    assert!(
        rust_html.as_bytes() == want.as_slice(),
        "{prefix} doc {idx}: Rust output differs from Python.\n--- rust ---\n{rust_html}\n--- python ---\n{}",
        String::from_utf8_lossy(&want)
    );
}

#[test]
fn basic_differential_all_docs() {
    let m = manifest();
    let mut r = StoretleReader::open(fixtures().join("basic.storetle"), Some(read_dict()))
        .expect("open basic.storetle");
    assert_eq!(r.doc_count().to_string(), m["basic.doc_count"]);
    assert_eq!(r.chunk_count().to_string(), m["basic.chunk_count"]);
    assert!(!r.has_embedded_dict());

    // Random access path
    for i in 0..r.doc_count() {
        let html = r.get(i).unwrap_or_else(|e| panic!("get({i}): {e}"));
        assert_doc_matches("basic", i, &html);
    }
    // Iterator path must agree with random access
    let via_iter: Vec<String> = r.iter_docs().collect::<Result<_, _>>().expect("iter");
    assert_eq!(via_iter.len() as u64, r.doc_count());
    for (i, html) in via_iter.iter().enumerate() {
        assert_doc_matches("basic", i as u64, html);
    }
}

#[test]
fn multi_chunk_random_access() {
    let m = manifest();
    let mut r = StoretleReader::open(fixtures().join("multi.storetle"), Some(read_dict()))
        .expect("open multi.storetle");
    assert_eq!(r.doc_count().to_string(), m["multi.doc_count"]);
    assert_eq!(r.chunk_count().to_string(), m["multi.chunk_count"]);
    assert!(r.chunk_count() > 1, "fixture must span multiple chunks");

    let samples: Vec<u64> = m["multi.samples"]
        .split(',')
        .map(|s| s.parse().unwrap())
        .collect();
    // Access samples out of order to exercise chunk switching
    for &i in samples.iter().rev() {
        let html = r.get(i).unwrap_or_else(|e| panic!("get({i}): {e}"));
        assert_doc_matches("multi", i, &html);
    }
    for &i in &samples {
        let html = r.get(i).unwrap();
        assert_doc_matches("multi", i, &html);
    }
}

#[test]
fn embedded_dict_needs_no_external_dict() {
    let m = manifest();
    let mut r = StoretleReader::open(fixtures().join("embedded.storetle"), None)
        .expect("open embedded.storetle");
    assert_eq!(r.doc_count().to_string(), m["embedded.doc_count"]);
    assert!(r.has_embedded_dict());
    for i in 0..r.doc_count() {
        let html = r.get(i).unwrap_or_else(|e| panic!("get({i}): {e}"));
        assert_doc_matches("embedded", i, &html);
    }
}

#[test]
fn missing_dictionary_is_a_clear_error() {
    let mut r = StoretleReader::open(fixtures().join("basic.storetle"), None)
        .expect("open should succeed without dict (index only)");
    assert_eq!(r.doc_count(), 12);
    let err = r
        .get(0)
        .expect_err("decompression must fail without the dictionary");
    let msg = err.to_string();
    assert!(
        matches!(err, storetle::Error::DictionaryRequired) || msg.contains("dictionary"),
        "unhelpful error without dictionary: {msg}"
    );
}

#[test]
fn out_of_range_index() {
    let mut r = StoretleReader::open(fixtures().join("basic.storetle"), Some(read_dict())).unwrap();
    assert!(matches!(
        r.get(9999),
        Err(storetle::Error::OutOfRange { .. })
    ));
}

#[test]
fn bad_magic_rejected() {
    let err = StoretleReader::from_bytes(b"NOPE0000000000000000".to_vec(), None)
        .err()
        .expect("must reject");
    assert!(matches!(err, storetle::Error::BadMagic(_)));
}
