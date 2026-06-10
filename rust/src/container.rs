//! `.storetle` container parsing: header, chunk index (via footer), chunk
//! decompression, and document access.

use std::io::{Cursor, Read, Seek, SeekFrom};

use crate::codec;
use crate::decode::decode_doc;
use crate::{Error, Result};

const STREAM_MAGIC: [u8; 4] = *b"STRL";
const STREAM_VERSION: u8 = 2;

/// One entry of the chunk index.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChunkInfo {
    /// Absolute file offset of the chunk header.
    pub file_offset: u64,
    /// Number of documents in this chunk.
    pub doc_count: u16,
    /// Total size of the encoded (uncompressed) document blobs in this chunk.
    pub orig_total: u32,
}

/// Reader for a `.storetle` archive over any `Read + Seek` source.
///
/// ```no_run
/// # fn main() -> storetle::Result<()> {
/// let dict = std::fs::read("cube_dict_v10.bin")?;
/// let mut r = storetle::StoretleReader::open("crawl.storetle", Some(dict))?;
/// println!("{} docs", r.doc_count());
/// let html = r.get(42)?; // random access: decompresses only one chunk
/// # let _ = html; Ok(())
/// # }
/// ```
pub struct StoretleReader<R: Read + Seek> {
    src: R,
    dict: Option<Vec<u8>>,
    embedded_dict: bool,
    index: Vec<ChunkInfo>,
    doc_count: u64,
    /// Cache of the most recently decompressed chunk: (chunk_idx, docs).
    chunk_cache: Option<(usize, Vec<Vec<u8>>)>,
}

impl StoretleReader<std::io::BufReader<std::fs::File>> {
    /// Open a `.storetle` file from disk.
    ///
    /// `dictionary`: external zstd dictionary bytes (`cube_dict_v10.bin`).
    /// If the file embeds its own dictionary, the embedded one is used unless
    /// `dictionary` is `Some`, which overrides it (matching the Python reader).
    pub fn open<P: AsRef<std::path::Path>>(path: P, dictionary: Option<Vec<u8>>) -> Result<Self> {
        let f = std::fs::File::open(path)?;
        Self::new(std::io::BufReader::new(f), dictionary)
    }
}

impl StoretleReader<Cursor<Vec<u8>>> {
    /// Construct a reader over an in-memory buffer (used by the wasm build).
    pub fn from_bytes(data: Vec<u8>, dictionary: Option<Vec<u8>>) -> Result<Self> {
        Self::new(Cursor::new(data), dictionary)
    }
}

impl<R: Read + Seek> StoretleReader<R> {
    /// Parse the header and chunk index from `src`.
    pub fn new(mut src: R, dictionary: Option<Vec<u8>>) -> Result<Self> {
        // --- Header ---
        src.seek(SeekFrom::Start(0))?;
        let mut magic = [0u8; 4];
        src.read_exact(&mut magic)?;
        if magic != STREAM_MAGIC {
            return Err(Error::BadMagic(magic));
        }
        let mut v = [0u8; 1];
        src.read_exact(&mut v)?;
        if v[0] != STREAM_VERSION {
            return Err(Error::BadVersion(v[0]));
        }
        let mut ds = [0u8; 4];
        src.read_exact(&mut ds)?;
        let dict_size = u32::from_be_bytes(ds) as usize;

        let embedded_dict = dict_size > 0;
        let mut dict: Option<Vec<u8>> = None;
        if embedded_dict {
            let mut d = vec![0u8; dict_size];
            src.read_exact(&mut d)?;
            dict = Some(d);
        }
        // Caller-supplied dictionary overrides the embedded one.
        if dictionary.is_some() {
            dict = dictionary;
        }

        // --- Footer (last 16 bytes) + index ---
        let file_len = src.seek(SeekFrom::End(0))?;
        if file_len < (9 + dict_size as u64 + 16) {
            return Err(Error::Corrupt("file too small for header + footer".into()));
        }
        src.seek(SeekFrom::End(-16))?;
        let mut footer = [0u8; 16];
        src.read_exact(&mut footer)?;
        let chunk_count = u64::from_be_bytes(footer[0..8].try_into().unwrap());
        let index_offset = u64::from_be_bytes(footer[8..16].try_into().unwrap());

        let index_bytes = chunk_count
            .checked_mul(14)
            .filter(|&n| {
                index_offset
                    .checked_add(n)
                    .is_some_and(|end| end <= file_len - 16)
            })
            .ok_or_else(|| Error::Corrupt("chunk index out of bounds".into()))?;

        src.seek(SeekFrom::Start(index_offset))?;
        let mut raw_index = vec![0u8; index_bytes as usize];
        src.read_exact(&mut raw_index)?;

        let mut index = Vec::with_capacity(chunk_count as usize);
        let mut doc_count: u64 = 0;
        for entry in raw_index.chunks_exact(14) {
            let info = ChunkInfo {
                file_offset: u64::from_be_bytes(entry[0..8].try_into().unwrap()),
                doc_count: u16::from_be_bytes(entry[8..10].try_into().unwrap()),
                orig_total: u32::from_be_bytes(entry[10..14].try_into().unwrap()),
            };
            doc_count += info.doc_count as u64;
            index.push(info);
        }

        Ok(StoretleReader {
            src,
            dict,
            embedded_dict,
            index,
            doc_count,
            chunk_cache: None,
        })
    }

    /// Total number of documents in the archive.
    pub fn doc_count(&self) -> u64 {
        self.doc_count
    }

    /// Number of chunks.
    pub fn chunk_count(&self) -> usize {
        self.index.len()
    }

    /// The chunk index.
    pub fn chunks(&self) -> &[ChunkInfo] {
        &self.index
    }

    /// True if the file carries its own embedded dictionary.
    pub fn has_embedded_dict(&self) -> bool {
        self.embedded_dict
    }

    /// True if a dictionary (embedded or supplied) is loaded.
    pub fn has_dict(&self) -> bool {
        self.dict.as_deref().is_some_and(|d| !d.is_empty())
    }

    /// Decompress chunk `chunk_idx` and split it into raw encoded doc blobs.
    pub fn read_chunk(&mut self, chunk_idx: usize) -> Result<&[Vec<u8>]> {
        if let Some((cached, _)) = &self.chunk_cache {
            if *cached == chunk_idx {
                // Borrow dance: re-take the cached value.
                return Ok(&self.chunk_cache.as_ref().unwrap().1);
            }
        }
        let info = *self
            .index
            .get(chunk_idx)
            .ok_or_else(|| Error::Corrupt(format!("chunk index {chunk_idx} out of range")))?;

        self.src.seek(SeekFrom::Start(info.file_offset))?;
        let mut hdr = [0u8; 10];
        self.src.read_exact(&mut hdr)?;
        let doc_count = u16::from_be_bytes(hdr[0..2].try_into().unwrap()) as usize;
        let orig_total = u32::from_be_bytes(hdr[2..6].try_into().unwrap()) as usize;
        let comp_size = u32::from_be_bytes(hdr[6..10].try_into().unwrap()) as usize;
        if doc_count != info.doc_count as usize || orig_total != info.orig_total as usize {
            return Err(Error::Corrupt(format!(
                "chunk {chunk_idx} header disagrees with index"
            )));
        }

        let mut size_bytes = vec![0u8; doc_count * 4];
        self.src.read_exact(&mut size_bytes)?;
        let sizes: Vec<usize> = size_bytes
            .chunks_exact(4)
            .map(|b| u32::from_be_bytes(b.try_into().unwrap()) as usize)
            .collect();
        if sizes.iter().sum::<usize>() != orig_total {
            return Err(Error::Corrupt(format!(
                "chunk {chunk_idx}: per-doc sizes do not sum to orig_total"
            )));
        }

        let mut compressed = vec![0u8; comp_size];
        self.src.read_exact(&mut compressed)?;

        let blob = codec::decompress(&compressed, self.dict.as_deref(), orig_total)?;

        let mut docs = Vec::with_capacity(doc_count);
        let mut pos = 0usize;
        for sz in sizes {
            docs.push(blob[pos..pos + sz].to_vec());
            pos += sz;
        }
        self.chunk_cache = Some((chunk_idx, docs));
        Ok(&self.chunk_cache.as_ref().unwrap().1)
    }

    /// Map a global document index to (chunk index, index within chunk).
    pub fn locate(&self, doc_idx: u64) -> Result<(usize, usize)> {
        if doc_idx >= self.doc_count {
            return Err(Error::OutOfRange {
                index: doc_idx,
                count: self.doc_count,
            });
        }
        let mut running: u64 = 0;
        for (ci, info) in self.index.iter().enumerate() {
            let dc = info.doc_count as u64;
            if doc_idx < running + dc {
                return Ok((ci, (doc_idx - running) as usize));
            }
            running += dc;
        }
        unreachable!("doc_count is the sum of chunk doc counts");
    }

    /// Return the raw encoded blob for one document (random access:
    /// decompresses only the containing chunk, with a one-chunk cache).
    pub fn get_raw(&mut self, doc_idx: u64) -> Result<Vec<u8>> {
        let (ci, wi) = self.locate(doc_idx)?;
        let docs = self.read_chunk(ci)?;
        Ok(docs[wi].clone())
    }

    /// Return the decoded HTML for one document.
    pub fn get(&mut self, doc_idx: u64) -> Result<String> {
        let (ci, wi) = self.locate(doc_idx)?;
        let docs = self.read_chunk(ci)?;
        decode_doc(&docs[wi])
    }

    /// Iterate over all documents in order, yielding decoded HTML.
    ///
    /// Chunks are decompressed one at a time (sequential access pattern).
    pub fn iter_docs(&mut self) -> DocIter<'_, R> {
        DocIter {
            reader: self,
            chunk_idx: 0,
            within: 0,
        }
    }
}

/// Iterator over decoded documents (see [`StoretleReader::iter_docs`]).
pub struct DocIter<'a, R: Read + Seek> {
    reader: &'a mut StoretleReader<R>,
    chunk_idx: usize,
    within: usize,
}

impl<R: Read + Seek> Iterator for DocIter<'_, R> {
    type Item = Result<String>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if self.chunk_idx >= self.reader.chunk_count() {
                return None;
            }
            let docs = match self.reader.read_chunk(self.chunk_idx) {
                Ok(d) => d,
                Err(e) => {
                    self.chunk_idx = usize::MAX; // poison: stop iterating
                    return Some(Err(e));
                }
            };
            if self.within < docs.len() {
                let raw = &docs[self.within];
                let item = decode_doc(raw);
                self.within += 1;
                return Some(item);
            }
            self.chunk_idx += 1;
            self.within = 0;
        }
    }
}
