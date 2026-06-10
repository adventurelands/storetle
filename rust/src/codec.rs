//! zstd decompression with an optional dictionary, behind a feature switch.
//!
//! - `zstd-native` (default): the `zstd` crate (libzstd C bindings).
//! - `zstd-pure`: the pure-Rust `ruzstd` crate — used for wasm32 builds.
//!   ruzstd >= 0.8 decodes ZDICT-format dictionaries (`Dictionary::decode_dict`)
//!   and selects them by the dictionary id recorded in the frame header.
//!
//! If both features are enabled, the native backend wins.

use crate::{Error, Result};

/// Magic number at the start of a ZDICT-trained dictionary.
const ZDICT_MAGIC: [u8; 4] = [0x37, 0xA4, 0x30, 0xEC];

/// Returns true if `dict` looks like a ZDICT-format dictionary.
pub fn is_zdict(dict: &[u8]) -> bool {
    dict.len() >= 8 && dict[..4] == ZDICT_MAGIC
}

/// Decompress one zstd frame, using `dict` if provided.
///
/// `expected_size` is the known uncompressed size (from the chunk header);
/// it is used to pre-size buffers and to sanity-check the result.
pub fn decompress(data: &[u8], dict: Option<&[u8]>, expected_size: usize) -> Result<Vec<u8>> {
    let out = decompress_impl(data, dict, expected_size)?;
    if out.len() != expected_size {
        return Err(Error::Corrupt(format!(
            "chunk decompressed to {} bytes, header says {}",
            out.len(),
            expected_size
        )));
    }
    Ok(out)
}

#[cfg(feature = "zstd-native")]
fn decompress_impl(data: &[u8], dict: Option<&[u8]>, expected_size: usize) -> Result<Vec<u8>> {
    let mut dec = match dict {
        Some(d) if !d.is_empty() => zstd::bulk::Decompressor::with_dictionary(d)
            .map_err(|e| Error::Zstd(format!("loading dictionary: {e}")))?,
        _ => zstd::bulk::Decompressor::new().map_err(|e| Error::Zstd(e.to_string()))?,
    };
    dec.decompress(data, expected_size).map_err(|e| {
        let msg = e.to_string();
        if msg.contains("Dictionary mismatch") {
            return Error::DictionaryRequired;
        }
        Error::Zstd(msg)
    })
}

#[cfg(all(feature = "zstd-pure", not(feature = "zstd-native")))]
fn decompress_impl(data: &[u8], dict: Option<&[u8]>, expected_size: usize) -> Result<Vec<u8>> {
    use ruzstd::decoding::{BlockDecodingStrategy, Dictionary, FrameDecoder};

    let mut decoder = FrameDecoder::new();
    if let Some(d) = dict {
        if !d.is_empty() {
            let parsed = Dictionary::decode_dict(d)
                .map_err(|e| Error::Zstd(format!("loading dictionary: {e:?}")))?;
            decoder
                .add_dict(parsed)
                .map_err(|e| Error::Zstd(format!("registering dictionary: {e:?}")))?;
        }
    }

    let mut cursor = data;
    decoder.reset(&mut cursor).map_err(|e| {
        let msg = format!("{e:?}");
        if msg.contains("DictNotProvided") {
            return Error::DictionaryRequired;
        }
        Error::Zstd(msg)
    })?;
    decoder
        .decode_blocks(&mut cursor, BlockDecodingStrategy::All)
        .map_err(|e| Error::Zstd(format!("{e:?}")))?;
    let mut out = Vec::with_capacity(expected_size);
    decoder
        .collect_to_writer(&mut out)
        .map_err(|e| Error::Zstd(format!("{e:?}")))?;
    Ok(out)
}

#[cfg(not(any(feature = "zstd-native", feature = "zstd-pure")))]
fn decompress_impl(_data: &[u8], _dict: Option<&[u8]>, _expected_size: usize) -> Result<Vec<u8>> {
    compile_error!("enable feature `zstd-native` or `zstd-pure`");
}
