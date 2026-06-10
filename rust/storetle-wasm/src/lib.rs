//! WebAssembly bindings for the `.storetle` reader.
//!
//! Uses the pure-Rust `ruzstd` backend (`zstd-pure` feature of the `storetle`
//! crate), so no C toolchain is involved in the wasm build. ruzstd >= 0.8
//! fully supports ZDICT-format dictionaries (verified by the native
//! differential test run with `--features zstd-pure`).

use std::io::Cursor;

use storetle::StoretleReader;
use wasm_bindgen::prelude::*;

/// A parsed `.storetle` archive held in memory.
#[wasm_bindgen]
pub struct WasmReader {
    inner: StoretleReader<Cursor<Vec<u8>>>,
}

#[wasm_bindgen]
impl WasmReader {
    /// Parse an archive from bytes. `dict` is the external zstd dictionary
    /// (`cube_dict_v10.bin`); pass `undefined`/`null` for files with an
    /// embedded dictionary.
    #[wasm_bindgen(constructor)]
    pub fn new(data: Vec<u8>, dict: Option<Vec<u8>>) -> Result<WasmReader, JsError> {
        let inner = StoretleReader::from_bytes(data, dict.filter(|d| !d.is_empty()))
            .map_err(|e| JsError::new(&e.to_string()))?;
        Ok(WasmReader { inner })
    }

    /// Total number of documents.
    #[wasm_bindgen(getter)]
    pub fn doc_count(&self) -> f64 {
        self.inner.doc_count() as f64
    }

    /// Number of chunks.
    #[wasm_bindgen(getter)]
    pub fn chunk_count(&self) -> u32 {
        self.inner.chunk_count() as u32
    }

    /// True if the file carries an embedded dictionary.
    #[wasm_bindgen(getter)]
    pub fn has_embedded_dict(&self) -> bool {
        self.inner.has_embedded_dict()
    }

    /// True if a dictionary (embedded or supplied) is loaded.
    #[wasm_bindgen(getter)]
    pub fn has_dict(&self) -> bool {
        self.inner.has_dict()
    }

    /// Decode document `index` to HTML. Decompresses only the containing
    /// chunk (with a one-chunk cache, so sequential viewing is cheap).
    pub fn get_html(&mut self, index: u32) -> Result<String, JsError> {
        self.inner
            .get(index as u64)
            .map_err(|e| JsError::new(&e.to_string()))
    }
}
