//! NodeOp document decoder — reconstructs HTML from the v2 two-stream encoding.
//!
//! A document blob is `ss_size(u32 BE) + struct_stream + content_stream`.
//!
//! Struct stream: a sequence of node ops. For `T_OPEN`/`T_SELFCLOSE`:
//! `op(1) + tag_id(1) + attr_count(1) + attr_ids(attr_count x 1)`; other ops
//! are a single byte. Tag/attr names and all values live in the content
//! stream, encoded as string refs:
//!
//! ```text
//! 0x00-0xFB      shared-vocab string id (1 byte)
//! 0xFC           class token list: count(1) + count x string-ref, joined by ' '
//! 0xFD           inline string: len(1 byte if <=254, else 0xFF + u32 BE) + UTF-8
//! 0xFE           None (boolean attribute / no value)
//! 0xFF HH LL     shared-vocab string id 252+ (big-endian u16)
//! ```
//!
//! The output formatting matches the Python reference decoder byte-for-byte:
//! two-space indentation for tags, text/comment/doctype lines unindented,
//! lines joined with `\n`.

use crate::vocab::{ATTRS, SHARED_STRINGS, TAGS, UNKNOWN_ID};
use crate::{Error, Result};

// Node op codes (encoder.py).
const T_OPEN: u8 = 0x01;
const T_CLOSE: u8 = 0x02;
const T_TEXT: u8 = 0x03;
const T_DOCTYPE: u8 = 0x04;
const T_COMMENT: u8 = 0x05;
const T_SELFCLOSE: u8 = 0x06;
const T_RAWTEXT: u8 = 0x07;

/// Sequential reader over one of the two byte streams.
struct Stream<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Stream<'a> {
    fn new(data: &'a [u8]) -> Self {
        Stream { data, pos: 0 }
    }

    fn read_byte(&mut self) -> Result<u8> {
        let b = *self
            .data
            .get(self.pos)
            .ok_or_else(|| Error::Corrupt("unexpected end of stream".into()))?;
        self.pos += 1;
        Ok(b)
    }

    fn read_slice(&mut self, len: usize) -> Result<&'a [u8]> {
        let end = self
            .pos
            .checked_add(len)
            .filter(|&e| e <= self.data.len())
            .ok_or_else(|| Error::Corrupt("string runs past end of stream".into()))?;
        let s = &self.data[self.pos..end];
        self.pos = end;
        Ok(s)
    }

    /// Read a string ref (decoder.py `_Stream.read_string`).
    /// Returns `None` for the 0xFE no-value marker.
    fn read_string(&mut self) -> Result<Option<String>> {
        let b = self.read_byte()?;
        match b {
            0xFE => Ok(None),
            0xFF => {
                let hi = self.read_byte()? as usize;
                let lo = self.read_byte()? as usize;
                let sid = (hi << 8) | lo;
                let s = SHARED_STRINGS.get(sid).ok_or_else(|| {
                    Error::Corrupt(format!("shared string id {sid} out of range"))
                })?;
                Ok(Some((*s).to_string()))
            }
            0xFD => {
                let b1 = self.read_byte()?;
                let len = if b1 <= 254 {
                    b1 as usize
                } else {
                    let raw = self.read_slice(4)?;
                    u32::from_be_bytes([raw[0], raw[1], raw[2], raw[3]]) as usize
                };
                let bytes = self.read_slice(len)?;
                let s = std::str::from_utf8(bytes)
                    .map_err(|e| Error::Corrupt(format!("invalid UTF-8 in inline string: {e}")))?;
                Ok(Some(s.to_string()))
            }
            0xFC => {
                let count = self.read_byte()?;
                let mut tokens: Vec<String> = Vec::with_capacity(count as usize);
                for _ in 0..count {
                    if let Some(t) = self.read_string()? {
                        tokens.push(t);
                    }
                }
                Ok(Some(tokens.join(" ")))
            }
            id => {
                let s = SHARED_STRINGS
                    .get(id as usize)
                    .ok_or_else(|| Error::Corrupt(format!("shared string id {id} out of range")))?;
                Ok(Some((*s).to_string()))
            }
        }
    }
}

/// Escape a text node exactly like the Python decoder
/// (`&`→`&amp;`, `<`→`&lt;`, `>`→`&gt;`).
fn escape_text(out: &mut String, s: &str) {
    for ch in s.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            c => out.push(c),
        }
    }
}

/// Escape an attribute value exactly like the Python decoder
/// (`&`→`&amp;`, `"`→`&quot;`, `<`→`&lt;`).
fn escape_attr(out: &mut String, s: &str) {
    for ch in s.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '"' => out.push_str("&quot;"),
            '<' => out.push_str("&lt;"),
            c => out.push(c),
        }
    }
}

fn push_indent(out: &mut String, indent: usize) {
    for _ in 0..indent {
        out.push_str("  ");
    }
}

/// Decode one encoded document blob (as stored inside a chunk) to HTML.
///
/// Mirrors `stream._decode_doc` in the Python reference implementation; the
/// output is byte-for-byte identical for well-formed inputs.
pub fn decode_doc(raw: &[u8]) -> Result<String> {
    if raw.len() < 4 {
        return Err(Error::Corrupt("document blob shorter than 4 bytes".into()));
    }
    let ss_len = u32::from_be_bytes([raw[0], raw[1], raw[2], raw[3]]) as usize;
    if 4 + ss_len > raw.len() {
        return Err(Error::Corrupt(format!(
            "struct stream length {ss_len} exceeds blob size {}",
            raw.len()
        )));
    }
    let mut ss = Stream::new(&raw[4..4 + ss_len]);
    let mut cs = Stream::new(&raw[4 + ss_len..]);

    let mut out = String::with_capacity(raw.len() * 2);
    let mut first_line = true;
    let mut indent: usize = 0;
    let mut tag_stack: Vec<String> = Vec::new();

    macro_rules! newline {
        () => {
            if first_line {
                first_line = false;
            } else {
                out.push('\n');
            }
        };
    }

    while ss.pos < ss.data.len() {
        let nt = ss.read_byte()?;
        match nt {
            T_OPEN | T_SELFCLOSE => {
                let tag_id = ss.read_byte()?;
                let tag: String = if tag_id == UNKNOWN_ID {
                    cs.read_string()?.unwrap_or_default()
                } else {
                    match TAGS.get(tag_id as usize) {
                        Some(t) => (*t).to_string(),
                        None => format!("?{tag_id}"),
                    }
                };
                let attr_count = ss.read_byte()?;
                newline!();
                push_indent(&mut out, indent);
                out.push('<');
                out.push_str(&tag);
                for _ in 0..attr_count {
                    let aid = ss.read_byte()?;
                    let aname: String = if aid == UNKNOWN_ID {
                        cs.read_string()?.unwrap_or_default()
                    } else {
                        match ATTRS.get(aid as usize) {
                            Some(a) => (*a).to_string(),
                            None => format!("?{aid}"),
                        }
                    };
                    let val = cs.read_string()?;
                    out.push(' ');
                    out.push_str(&aname);
                    if let Some(v) = val {
                        out.push_str("=\"");
                        escape_attr(&mut out, &v);
                        out.push('"');
                    }
                }
                out.push('>');
                if nt == T_OPEN {
                    tag_stack.push(tag);
                    indent += 1;
                }
            }
            T_CLOSE => {
                let tag = tag_stack.pop().unwrap_or_default();
                indent = indent.saturating_sub(1);
                newline!();
                push_indent(&mut out, indent);
                out.push_str("</");
                out.push_str(&tag);
                out.push('>');
            }
            T_TEXT => {
                let t = cs.read_string()?.unwrap_or_default();
                newline!();
                escape_text(&mut out, &t);
            }
            T_RAWTEXT => {
                let t = cs.read_string()?.unwrap_or_default();
                newline!();
                out.push_str(&t);
            }
            T_DOCTYPE => {
                let t = cs.read_string()?.unwrap_or_default();
                newline!();
                out.push_str("<!");
                out.push_str(&t);
                out.push('>');
            }
            T_COMMENT => {
                let t = cs.read_string()?.unwrap_or_default();
                newline!();
                out.push_str("<!--");
                out.push_str(&t);
                out.push_str("-->");
            }
            other => {
                return Err(Error::Corrupt(format!("unknown node type {other:#04x}")));
            }
        }
    }

    Ok(out)
}
