# decoder.py  —  v4
# Two-stream decoder: structure stream + content stream, both zlib-compressed.
# v4: text node payloads (T_TEXT, T_DOCTYPE, T_COMMENT, T_RAWTEXT) are written
# inline in the content stream (0xFD marker) rather than via string table IDs.

import struct
import zlib
from .vocab import ID_TO_TAG, ID_TO_ATTR, VOID_ELEMENTS, UNKNOWN_ID, SHARED_STRINGS, SHARED_COUNT
from .encoder import (
    T_OPEN, T_CLOSE, T_TEXT, T_DOCTYPE, T_COMMENT, T_SELFCLOSE, T_RAWTEXT,
    MAGIC, VERSION,
)


class _Stream:
    """Lightweight wrapper around a decompressed bytes block for sequential reads."""
    def __init__(self, data: bytes):
        self._data = data
        self._pos  = 0

    def read_byte(self) -> int:
        b = self._data[self._pos];  self._pos += 1
        return b

    def read_sid(self):
        """
        Varint string ID:
          0x00-0xFB  → that value (IDs 0-251, 1 byte)
          0xFC       → class token list — raises ValueError
                       (use read_string() for positions that may contain class lists)
          0xFD       → inline string — raises ValueError
                       (use read_string() for positions that may contain inline strings)
          0xFE       → None (boolean attribute, no value)
          0xFF HH LL → big-endian uint16 ID (IDs 252+, 3 bytes total)
        """
        b = self.read_byte()
        if b == 0xFE:
            return None
        if b == 0xFF:
            hi = self.read_byte()
            lo = self.read_byte()
            return (hi << 8) | lo
        if b == 0xFD:
            raise ValueError('Unexpected inline string marker in read_sid; use read_string()')
        if b == 0xFC:
            raise ValueError('Unexpected class-list marker in read_sid; use read_string()')
        return b

    def read_string(self, all_strings):
        """
        Read a string value from the content stream. Handles:
          0x00-0xFB  → 1-byte string ID (IDs 0-251)
          0xFC       → class token list: count (1B) + N token reads → joined with spaces
          0xFD       → inline string: 4-byte length + UTF-8 bytes
          0xFE       → None (boolean / no-value attribute)
          0xFF HH LL → 3-byte string ID (IDs 252+)
        """
        b = self.read_byte()
        if b == 0xFE:
            return None
        if b == 0xFF:
            hi = self.read_byte()
            lo = self.read_byte()
            sid = (hi << 8) | lo
            return all_strings[sid]
        if b == 0xFD:
            b1 = self.read_byte()
            if b1 <= 254:
                length = b1
            else:                 # 0xFF escape for strings >= 255 bytes
                length = struct.unpack_from('>I', self._data, self._pos)[0]
                self._pos += 4
            s = self._data[self._pos:self._pos + length].decode('utf-8')
            self._pos += length
            return s
        if b == 0xFC:
            count = self.read_byte()
            tokens = []
            for _ in range(count):
                t = self.read_string(all_strings)
                if t is not None:
                    tokens.append(t)
            return ' '.join(tokens)
        # 0x00-0xFB: 1-byte ID
        return all_strings[b]


def decode(cube_bytes: bytes) -> str:
    pos = 0

    # --- Header ---
    magic = cube_bytes[pos:pos+4];  pos += 4
    if magic != MAGIC:
        raise ValueError(f'Not a valid .cube file (magic: {magic!r})')

    version = cube_bytes[pos];  pos += 1
    if version != VERSION:
        raise ValueError(f'Decoder handles v{VERSION}, file is v{version}. Re-encode the file.')
    # Note: v5 adds class-list encoding (0xFC). Files encoded with v4 cannot
    # be decoded by this decoder without re-encoding.

    node_count   = struct.unpack_from('>I', cube_bytes, pos)[0];  pos += 4
    st_size      = struct.unpack_from('>I', cube_bytes, pos)[0];  pos += 4
    struct_size  = struct.unpack_from('>I', cube_bytes, pos)[0];  pos += 4
    content_size = struct.unpack_from('>I', cube_bytes, pos)[0];  pos += 4

    # --- Decompress and read string table ---
    st_compressed = cube_bytes[pos:pos + st_size];  pos += st_size
    raw_table = zlib.decompress(st_compressed)

    tpos = 0
    string_count = struct.unpack_from('>I', raw_table, tpos)[0];  tpos += 4
    file_strings = []
    for _ in range(string_count):
        length = struct.unpack_from('>I', raw_table, tpos)[0];  tpos += 4
        s = raw_table[tpos:tpos + length].decode('utf-8');  tpos += length
        file_strings.append(s)

    all_strings = SHARED_STRINGS + file_strings

    # --- Decompress the two body streams ---
    struct_stream  = _Stream(zlib.decompress(cube_bytes[pos:pos + struct_size]))
    pos += struct_size
    content_stream = _Stream(zlib.decompress(cube_bytes[pos:pos + content_size]))

    # --- Reconstruct HTML ---
    output    = []
    indent    = 0
    tag_stack = []   # tracks open tags so T_CLOSE needs no tag_id in stream

    for _ in range(node_count):
        node_type = struct_stream.read_byte()

        if node_type in (T_OPEN, T_SELFCLOSE):
            tag_id = struct_stream.read_byte()

            if tag_id == UNKNOWN_ID:
                tag = content_stream.read_string(all_strings)
            else:
                tag = ID_TO_TAG.get(tag_id, f'unknown_{tag_id}')

            attr_count = struct_stream.read_byte()
            attrs_str  = ''

            for _ in range(attr_count):
                attr_id = struct_stream.read_byte()

                if attr_id == UNKNOWN_ID:
                    attr_name = content_stream.read_string(all_strings)
                else:
                    attr_name = ID_TO_ATTR.get(attr_id, f'unknown_attr_{attr_id}')

                attr_value = content_stream.read_string(all_strings)

                if attr_value is None:
                    attrs_str += f' {attr_name}'
                else:
                    escaped = (attr_value
                               .replace('&', '&amp;')
                               .replace('"', '&quot;')
                               .replace('<', '&lt;'))
                    attrs_str += f' {attr_name}="{escaped}"'

            if node_type == T_SELFCLOSE:
                output.append(f'{"  " * indent}<{tag}{attrs_str}>')
            else:
                output.append(f'{"  " * indent}<{tag}{attrs_str}>')
                tag_stack.append(tag)
                indent += 1

        elif node_type == T_CLOSE:
            # No tag_id in stream — pop the open-tag stack
            tag    = tag_stack.pop() if tag_stack else ''
            indent = max(0, indent - 1)
            output.append(f'{"  " * indent}</{tag}>')

        elif node_type == T_TEXT:
            raw = content_stream.read_string(all_strings) or ''
            output.append(raw.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

        elif node_type == T_RAWTEXT:
            output.append(content_stream.read_string(all_strings) or '')

        elif node_type == T_DOCTYPE:
            output.append(f'<!{content_stream.read_string(all_strings) or ""}>')

        elif node_type == T_COMMENT:
            output.append(f'<!--{content_stream.read_string(all_strings) or ""}-->')

        else:
            raise ValueError(f'Unknown node type {node_type:#x}')

    return '\n'.join(output)


def decode_file(input_path: str, output_path: str) -> dict:
    with open(input_path, 'rb') as f:
        cube_bytes = f.read()

    html_text = decode(cube_bytes)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_text)

    node_count = struct.unpack_from('>I', cube_bytes, 5)[0]

    return {
        'cube_size':  len(cube_bytes),
        'html_size':  len(html_text.encode('utf-8')),
        'node_count': node_count,
    }
