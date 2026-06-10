//! Reader for the `.storetle` archive format.
//!
//! A `.storetle` file is a streaming container of HTML documents:
//!
//! ```text
//! Header : b"STRL" + version(1, =2) + dict_size(u32 BE) + [dict bytes if embedded]
//! Chunks : repeated { doc_count(u16 BE) + orig_total(u32 BE) + comp_size(u32 BE)
//!                     + per_doc_encoded_sizes(doc_count x u32 BE)
//!                     + zstd-compressed blob (dictionary compression) }
//! Index  : at index_offset, chunk_count entries of
//!          { file_offset(u64 BE) + doc_count(u16 BE) + orig_total(u32 BE) } = 14 bytes
//! Footer : last 16 bytes = chunk_count(u64 BE) + index_offset(u64 BE)
//! ```
//!
//! Each document inside a chunk blob is `ss_size(u32 BE) + struct_stream +
//! content_stream` — the "NodeOp" encoding decoded by [`decode_doc`].
//!
//! The zstd dictionary (`cube_dict_v10.bin`) is a codec parameter: by default
//! it is *not* embedded in files and must be supplied by the caller.

pub mod codec;
pub mod container;
pub mod decode;
pub mod vocab;

pub use container::{ChunkInfo, StoretleReader};
pub use decode::decode_doc;

/// Errors produced while reading a `.storetle` file.
#[derive(Debug)]
pub enum Error {
    /// Underlying I/O failure.
    Io(std::io::Error),
    /// The file is not a `.storetle` archive (bad magic).
    BadMagic([u8; 4]),
    /// Unsupported container version.
    BadVersion(u8),
    /// The file is structurally invalid (truncated, inconsistent index, ...).
    Corrupt(String),
    /// zstd decompression failed.
    Zstd(String),
    /// The chunk needs a dictionary but none was provided.
    DictionaryRequired,
    /// Document index out of range.
    OutOfRange { index: u64, count: u64 },
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "i/o error: {e}"),
            Error::BadMagic(m) => write!(f, "not a .storetle file (magic: {m:?})"),
            Error::BadVersion(v) => write!(f, "unsupported .storetle version {v} (reader supports v2)"),
            Error::Corrupt(msg) => write!(f, "corrupt .storetle file: {msg}"),
            Error::Zstd(msg) => write!(f, "zstd decompression failed: {msg}"),
            Error::DictionaryRequired => write!(
                f,
                "this file was compressed with an external dictionary (cube_dict_v10.bin); supply it"
            ),
            Error::OutOfRange { index, count } => {
                write!(f, "document index {index} out of range (doc_count = {count})")
            }
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;
