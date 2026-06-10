# folder.py — HTMLCUBE folder encoder v10
#
# v7: corpus string table removed; brotli handles cross-file repetition.
# v8: delta encoding (REMOVED — pre-computing delta disrupts brotli LZ77 which
#     already handles cross-file repetition more efficiently on its own).
# v9: simple concatenation of all file streams + brotli q11 lgwin=24.
#     No delta, no per-file string tables, no format complexity.
# v10: switched to zstd level=19 + trained binary dictionary (cube_dict_v9.bin).
#      The dictionary is a codec artifact (shipped with the decoder, not in pack).
#
# Format (PACK_VERSION = 10):
#   header    : CPAK(4) + version(1) + file_count(4) + ss_csize(4) + cs_csize(4)
#   directory : fname_len(2) + fname + node_count(4) +
#               ss_start(4) + ss_len(4) + cs_start(4) + cs_len(4)
#   global struct stream  (zstd l19 + cube dict)
#   global content stream (zstd l19 + cube dict)

import struct
from pathlib import Path

from .vocab import (
    TAG_TO_ID, ATTR_TO_ID, VOID_ELEMENTS, UNKNOWN_ID,
    SHARED_STRINGS, SHARED_STR_TO_ID, SHARED_COUNT,
)
from .encoder import (
    HTMLTokenizer,
    T_OPEN, T_CLOSE, T_TEXT, T_DOCTYPE, T_COMMENT, T_SELFCLOSE, T_RAWTEXT,
    _write_sid, _write_inline, _write_class_list,
)
from . import brotli_compat as _br
from . import zstd_compat as _zs

PACK_MAGIC   = b'CPAK'
PACK_VERSION = 10

# Load the trained binary dictionary (codec artifact, not stored in pack files).
# Falls back to brotli if zstd or the dict file is unavailable.
_DICT_PATH = Path(__file__).parent / 'cube_dict_v10.bin'
_CUBE_DICT = _DICT_PATH.read_bytes() if (_DICT_PATH.exists() and _zs.available()) else b''

def _compress(data: bytes) -> bytes:
    if _CUBE_DICT:
        return _zs.compress_with_dict(data, _CUBE_DICT, level=22)
    return _br.compress(data)

def _decompress(data: bytes) -> bytes:
    if _CUBE_DICT:
        return _zs.decompress_with_dict(data, _CUBE_DICT)
    return _br.decompress(data)


# ---------------------------------------------------------------------------
# Stream builder
# ---------------------------------------------------------------------------

def _build_raw_streams(nodes):
    """Encode nodes into (struct_bytes, content_bytes)."""
    def wref(buf, s):
        if s in SHARED_STR_TO_ID:
            _write_sid(buf, SHARED_STR_TO_ID[s])
        else:
            _write_inline(buf, s)

    ss = bytearray()
    cs = bytearray()

    for node_type, tag, payload in nodes:
        if node_type in (T_OPEN, T_SELFCLOSE):
            tag_id = TAG_TO_ID.get(tag, UNKNOWN_ID)
            ss.append(node_type); ss.append(tag_id)
            if tag_id == UNKNOWN_ID:
                wref(cs, tag)
            ss.append(len(payload))
            for attr_name, attr_value in payload:
                attr_name = attr_name.lower()
                attr_id = ATTR_TO_ID.get(attr_name, UNKNOWN_ID)
                ss.append(attr_id)
                if attr_id == UNKNOWN_ID:
                    wref(cs, attr_name)
                if attr_value is None:
                    _write_sid(cs, None)
                elif attr_name == 'class':
                    _write_class_list(cs, attr_value, wref)
                else:
                    wref(cs, attr_value)
        elif node_type == T_CLOSE:
            ss.append(T_CLOSE)
        elif node_type in (T_TEXT, T_DOCTYPE, T_COMMENT, T_RAWTEXT):
            ss.append(node_type)
            _write_inline(cs, payload)

    return bytes(ss), bytes(cs)


# ---------------------------------------------------------------------------
# Pack: folder → .cubepack
# ---------------------------------------------------------------------------

def pack(input_dir, output_path):
    html_files = sorted(Path(input_dir).glob('**/*.html'))
    if not html_files:
        raise ValueError(f'No .html files in {input_dir}')

    global_struct  = bytearray()
    global_content = bytearray()
    directory      = []
    total_original = 0

    for path in html_files:
        text = path.read_text(encoding='utf-8', errors='replace')
        total_original += len(text.encode('utf-8'))
        tok = HTMLTokenizer()
        tok.feed(text)

        ss, cs = _build_raw_streams(tok.nodes)
        ss_start = len(global_struct);  global_struct  += ss
        cs_start = len(global_content); global_content += cs

        fname = str(path.relative_to(input_dir))
        directory.append((fname, len(tok.nodes), ss_start, len(ss), cs_start, len(cs)))

    compressed_struct  = _compress(bytes(global_struct))
    compressed_content = _compress(bytes(global_content))

    # Header: CPAK(4) + ver(1) + file_count(4) + ss_csize(4) + cs_csize(4) = 17 bytes
    out = bytearray()
    out += PACK_MAGIC
    out.append(PACK_VERSION)
    out += struct.pack('>I', len(html_files))
    out += struct.pack('>I', len(compressed_struct))
    out += struct.pack('>I', len(compressed_content))

    for fname, node_count, ss_start, ss_len, cs_start, cs_len in directory:
        fname_b = fname.encode('utf-8')
        out += struct.pack('>H', len(fname_b)); out += fname_b
        out += struct.pack('>I', node_count)
        out += struct.pack('>I', ss_start)
        out += struct.pack('>I', ss_len)
        out += struct.pack('>I', cs_start)
        out += struct.pack('>I', cs_len)

    out += compressed_struct
    out += compressed_content

    Path(output_path).write_bytes(bytes(out))

    return {
        'file_count':    len(html_files),
        'total_original': total_original,
        'pack_size':     len(out),
        'savings_pct':   round((1 - len(out) / total_original) * 100, 1),
    }


# ---------------------------------------------------------------------------
# Unpack: .cubepack → folder of HTML files
# ---------------------------------------------------------------------------

def unpack(pack_path, output_dir):
    from .decoder import _Stream
    from .vocab import ID_TO_TAG, ID_TO_ATTR

    data = Path(pack_path).read_bytes()
    pos  = 0

    if data[pos:pos+4] != PACK_MAGIC:
        raise ValueError('Not a .cubepack file')
    pos += 4
    version    = data[pos];  pos += 1
    file_count = struct.unpack_from('>I', data, pos)[0];  pos += 4
    ss_csize   = struct.unpack_from('>I', data, pos)[0];  pos += 4
    cs_csize   = struct.unpack_from('>I', data, pos)[0];  pos += 4

    directory = []
    for _ in range(file_count):
        fname_len  = struct.unpack_from('>H', data, pos)[0];  pos += 2
        filename   = data[pos:pos+fname_len].decode('utf-8');  pos += fname_len
        node_count = struct.unpack_from('>I', data, pos)[0];  pos += 4
        ss_start   = struct.unpack_from('>I', data, pos)[0];  pos += 4
        ss_len     = struct.unpack_from('>I', data, pos)[0];  pos += 4
        cs_start   = struct.unpack_from('>I', data, pos)[0];  pos += 4
        cs_len     = struct.unpack_from('>I', data, pos)[0];  pos += 4
        directory.append((filename, node_count, ss_start, ss_len, cs_start, cs_len))

    global_struct  = _decompress(data[pos:pos+ss_csize]);  pos += ss_csize
    global_content = _decompress(data[pos:pos+cs_csize])

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    decoded_files = []

    for filename, node_count, ss_start, ss_len, cs_start, cs_len in directory:
        ss = _Stream(global_struct [ss_start: ss_start + ss_len])
        cs = _Stream(global_content[cs_start: cs_start + cs_len])

        output    = []
        indent    = 0
        tag_stack = []

        for _ in range(node_count):
            nt = ss.read_byte()

            if nt in (T_OPEN, T_SELFCLOSE):
                tag_id = ss.read_byte()
                tag    = cs.read_string(SHARED_STRINGS) if tag_id == UNKNOWN_ID \
                         else ID_TO_TAG.get(tag_id, f'?{tag_id}')
                ac     = ss.read_byte()
                attrs  = ''
                for _ in range(ac):
                    aid   = ss.read_byte()
                    aname = cs.read_string(SHARED_STRINGS) if aid == UNKNOWN_ID \
                            else ID_TO_ATTR.get(aid, f'?{aid}')
                    val   = cs.read_string(SHARED_STRINGS)
                    if val is None:
                        attrs += f' {aname}'
                    else:
                        e = val.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;')
                        attrs += f' {aname}="{e}"'
                output.append(f'{"  "*indent}<{tag}{attrs}>')
                if nt == T_OPEN:
                    tag_stack.append(tag)
                    indent += 1

            elif nt == T_CLOSE:
                tag    = tag_stack.pop() if tag_stack else ''
                indent = max(0, indent - 1)
                output.append(f'{"  "*indent}</{tag}>')

            elif nt == T_TEXT:
                raw = cs.read_string(SHARED_STRINGS) or ''
                output.append(raw.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'))
            elif nt == T_RAWTEXT:
                output.append(cs.read_string(SHARED_STRINGS) or '')
            elif nt == T_DOCTYPE:
                output.append(f'<!{cs.read_string(SHARED_STRINGS) or ""}>')
            elif nt == T_COMMENT:
                output.append(f'<!--{cs.read_string(SHARED_STRINGS) or ""}-->')

        html     = '\n'.join(output)
        out_path = Path(output_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding='utf-8')
        decoded_files.append(filename)

    return {
        'file_count': file_count,
        'pack_size':  len(data),
        'output_dir': output_dir,
        'files':      decoded_files,
    }
