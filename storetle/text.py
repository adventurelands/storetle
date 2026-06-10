# text.py — plain-text extraction straight from the NodeOp encoding.
#
# The encoded form already separates structure (struct stream) from content
# (content stream), so producing clean text never re-parses HTML: walk the
# opcodes, keep T_TEXT payloads, skip script/style bodies (T_RAWTEXT),
# comments and doctypes, and emit newlines at block-element boundaries.
#
# This consumes the content stream in exact lockstep with stream._decode_doc —
# every string the HTML decoder would read, this reads too, it just throws
# most of them away.

import re
import struct

from .decoder import _Stream
from .encoder import (T_OPEN, T_CLOSE, T_TEXT, T_DOCTYPE,
                      T_COMMENT, T_SELFCLOSE, T_RAWTEXT)
from .vocab import ID_TO_TAG, SHARED_STRINGS, UNKNOWN_ID

_BLOCK_TAGS = frozenset((
    'p', 'div', 'br', 'li', 'ul', 'ol', 'dl', 'dt', 'dd',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'tr', 'caption', 'thead', 'tbody',
    'section', 'article', 'aside', 'header', 'footer', 'main', 'nav',
    'blockquote', 'pre', 'figure', 'figcaption', 'hr', 'title',
))
_CELL_TAGS = frozenset(('td', 'th'))

_collapse_blank = re.compile(r'\n\s*\n+')
_collapse_space = re.compile(r'[ \t\f\v]+')


def decode_text(raw: bytes) -> bytes:
    """Extract plain text from one encoded document (the blob stored in a
    chunk), without reconstructing HTML."""
    ss_len  = struct.unpack_from('>I', raw, 0)[0]
    ss      = _Stream(raw[4: 4 + ss_len])
    cs      = _Stream(raw[4 + ss_len:])
    ss_data_len = ss_len

    out = []

    def boundary(tag):
        if tag in _BLOCK_TAGS:
            out.append('\n')
        elif tag in _CELL_TAGS:
            out.append('\t')

    while ss._pos < ss_data_len:
        nt = ss.read_byte()

        if nt in (T_OPEN, T_SELFCLOSE):
            tag_id = ss.read_byte()
            tag = cs.read_string(SHARED_STRINGS) if tag_id == UNKNOWN_ID \
                  else ID_TO_TAG.get(tag_id, '')
            ac = ss.read_byte()
            for _ in range(ac):
                aid = ss.read_byte()
                if aid == UNKNOWN_ID:
                    cs.read_string(SHARED_STRINGS)   # attr name — discard
                cs.read_string(SHARED_STRINGS)       # attr value — discard
            boundary(tag)

        elif nt == T_CLOSE:
            pass  # no payload; block boundary handled at open

        elif nt == T_TEXT:
            t = cs.read_string(SHARED_STRINGS)
            if t:
                out.append(t)

        elif nt in (T_RAWTEXT, T_DOCTYPE, T_COMMENT):
            cs.read_string(SHARED_STRINGS)           # script/style/meta — discard

    text = ''.join(out)
    text = _collapse_space.sub(' ', text)
    text = _collapse_blank.sub('\n', text)
    return text.strip().encode('utf-8')
