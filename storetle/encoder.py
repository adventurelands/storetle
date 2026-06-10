# encoder.py  —  v5
#
# Optimizations:
#
#   1. Shared vocabulary  — 1,394 common strings ship with the code; any HTML
#      that uses "flex", "items-center", "btn-primary" etc. gets a 1 or 3-byte
#      reference instead of an inline string.
#
#   2. Compressed string table  — file-specific strings are zlib-compressed.
#      Shrinks the string table 70-75%.
#
#   3. Variable-length string IDs (varint)  — IDs 0-251 cost 1 byte.
#      IDs 252+ cost 3 bytes (0xFF prefix + 2-byte ID).
#
#   4. Frequency threshold  — strings that appear only once are NOT stored in
#      the string table. They go inline (0xFD marker) in the content stream.
#
#   5. Class token splitting (v5)  — class="flex items-center gap-4" is split
#      into 3 individual token ID lookups instead of one 54-byte inline string.
#      Uses 0xFC marker: 0xFC | count (1B) | [token_id_or_inline × count].
#      Encoder splits class attr values; decoder reassembles with spaces.
#
# Content-stream byte encoding:
#   0x00–0xFB  (0–251) : string ID, 1 byte
#   0xFC               : class token list — next byte = count, then N token reads
#   0xFD               : inline string — 4-byte big-endian length + UTF-8 bytes
#   0xFE               : None / boolean attribute (no value)
#   0xFF HH LL         : string ID 252-65535, 3 bytes
#
# Format version = 5 (incompatible with v4).

import struct
import zlib
import logging as _logging
import threading as _threading
from html.parser import HTMLParser
from collections import Counter
from .vocab import (
    TAG_TO_ID, ATTR_TO_ID, VOID_ELEMENTS, UNKNOWN_ID,
    SHARED_STRINGS, SHARED_STR_TO_ID, SHARED_COUNT,
)

# ---------------------------------------------------------------------------
# Verified-path observability (process-wide counters, structured logging,
# optional Prometheus metrics). See docs/INTEGRATION.md §Observability.
# ---------------------------------------------------------------------------

_log = _logging.getLogger("storetle")

_VERIFIED_INIT_FAILURES = 0
_VERIFIED_FEED_FAILURES = 0
_VERIFIED_SUCCESSES = 0
_stats_lock = _threading.Lock()

try:
    from prometheus_client import Counter as _PromCounter
    _prom_init_failures = _PromCounter(
        "storetle_verified_init_failures_total",
        "Number of HTMLTokenizer instantiations where the verified path failed to initialize",
    )
    _prom_feed_failures = _PromCounter(
        "storetle_verified_feed_failures_total",
        "Number of HTMLTokenizer.feed() calls where the verified path raised mid-document",
    )
    _prom_successes = _PromCounter(
        "storetle_verified_successes_total",
        "Number of HTMLTokenizer.feed() calls routed through the verified path",
    )
    _HAVE_PROMETHEUS = True
except ImportError:
    _HAVE_PROMETHEUS = False


def _increment_init_failures():
    global _VERIFIED_INIT_FAILURES
    with _stats_lock:
        _VERIFIED_INIT_FAILURES += 1
    if _HAVE_PROMETHEUS:
        _prom_init_failures.inc()


def _increment_feed_failures():
    global _VERIFIED_FEED_FAILURES
    with _stats_lock:
        _VERIFIED_FEED_FAILURES += 1
    if _HAVE_PROMETHEUS:
        _prom_feed_failures.inc()


def _increment_successes():
    global _VERIFIED_SUCCESSES
    with _stats_lock:
        _VERIFIED_SUCCESSES += 1
    if _HAVE_PROMETHEUS:
        _prom_successes.inc()


def _current_tid():
    try:
        return _threading.get_ident()
    except Exception:
        return 0


def get_verified_stats():
    """Return a snapshot of verified-path counters.

    Returns a dict with keys: 'init_failures', 'feed_failures',
    'successes', 'verified_enabled' (True iff STORETLE_VERIFIED is set
    to a truthy value in the environment).
    """
    import os
    _flag = os.environ.get("STORETLE_VERIFIED", "").lower()
    with _stats_lock:
        return {
            "init_failures": _VERIFIED_INIT_FAILURES,
            "feed_failures": _VERIFIED_FEED_FAILURES,
            "successes": _VERIFIED_SUCCESSES,
            "verified_enabled": bool(_flag and _flag not in ("0", "false", "no")),
        }

try:
    from lxml import etree as _etree, html as _lxml_html
    _LXML = True
except ImportError:
    _LXML = False

# Node types
T_OPEN      = 0x01
T_CLOSE     = 0x02
T_TEXT      = 0x03   # normal HTML text — will be entity-escaped on decode
T_DOCTYPE   = 0x04
T_COMMENT   = 0x05
T_SELFCLOSE = 0x06
T_RAWTEXT   = 0x07   # raw content inside <script>/<style> — NO escaping on decode

MAGIC   = b'CUBE'
VERSION = 5

# Special marker: attribute has no value (e.g. <input disabled>)
BOOL_ATTR = 0xFEFE   # internal sentinel, never written directly


# ---------------------------------------------------------------------------
# Content-stream writers
# ---------------------------------------------------------------------------

def _write_sid(buf: bytearray, sid):
    """Write a string ID or boolean/None marker into buf."""
    if sid is BOOL_ATTR or sid is None:
        buf.append(0xFE)
    elif sid <= 251:          # IDs 0–251 → 1 byte  (0xFC freed for class-list)
        buf.append(sid)
    else:                     # IDs 252+ → 3 bytes
        buf.append(0xFF)
        buf += struct.pack('>H', sid)


def _write_inline(buf: bytearray, s: str):
    """Write an inline string: 0xFD + varint length + UTF-8 bytes.

    Length encoding (saves 3 bytes for the 99%+ of strings under 255 bytes):
      0x00–0xFE (0–254) : length as 1 byte
      0xFF              : next 4 bytes are big-endian uint32 length (long strings only)
    """
    encoded = s.encode('utf-8')
    n = len(encoded)
    buf.append(0xFD)
    if n <= 254:
        buf.append(n)
    else:
        buf.append(0xFF)
        buf += struct.pack('>I', n)
    buf += encoded


def _write_string_ref(buf: bytearray, s: str,
                      file_str_to_id: dict):
    """Write s into buf: shared ID, file ID, or inline if singleton."""
    if s in SHARED_STR_TO_ID:
        _write_sid(buf, SHARED_STR_TO_ID[s])
    elif s in file_str_to_id:
        _write_sid(buf, SHARED_COUNT + file_str_to_id[s])
    else:
        # Singleton — not in any table, write inline
        _write_inline(buf, s)


def _write_class_list(buf: bytearray, class_value: str, wref_fn):
    """Encode a class attribute value using the 0xFC token-list format.

    Format: 0xFC | count (1 byte) | [token_id_or_inline × count]

    Each space-separated token gets its own string ID lookup instead of the
    entire class string going inline. For a typical Tailwind class string like
    "flex items-center gap-4", this reduces ~54 inline bytes to ~5 bytes.

    Falls back to a regular wref for single-token values (saves the 2-byte
    class-list overhead when there's nothing to split).
    """
    tokens = class_value.split()
    if not tokens:
        _write_inline(buf, class_value)
        return
    if len(tokens) == 1:
        wref_fn(buf, tokens[0])
        return
    if len(tokens) > 255:
        # Pathological edge case — fall back to inline
        _write_inline(buf, class_value)
        return
    buf.append(0xFC)
    buf.append(len(tokens))
    for token in tokens:
        wref_fn(buf, token)


# ---------------------------------------------------------------------------
# HTML tokenizer
# ---------------------------------------------------------------------------

# Tags whose text content is raw — must never be entity-escaped on decode
RAWTEXT_TAGS = {'script', 'style'}


# ---------------------------------------------------------------------------
# Slow path: Python's built-in HTMLParser (fallback when lxml not available)
# ---------------------------------------------------------------------------

class _SlowTokenizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self._in_rawtext = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        node_type = T_SELFCLOSE if tag in VOID_ELEMENTS else T_OPEN
        self.nodes.append((node_type, tag, attrs))
        if tag in RAWTEXT_TAGS:
            self._in_rawtext = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in RAWTEXT_TAGS:
            self._in_rawtext = False
        if tag not in VOID_ELEMENTS:
            self.nodes.append((T_CLOSE, tag, None))

    def handle_data(self, data):
        if data.strip():
            node_type = T_RAWTEXT if self._in_rawtext else T_TEXT
            self.nodes.append((node_type, None, data))
        elif data and not data.strip():
            if ' ' in data and '\n' not in data:
                self.nodes.append((T_TEXT, None, ' '))

    def handle_decl(self, decl):
        self.nodes.append((T_DOCTYPE, None, decl))

    def handle_comment(self, data):
        self.nodes.append((T_COMMENT, None, data))


# ---------------------------------------------------------------------------
# Fast path: lxml C parser (~10-20x faster than HTMLParser)
# ---------------------------------------------------------------------------

def _tokenize_lxml(html):
    """Walk an lxml tree and produce the same node list format as _SlowTokenizer."""
    # Always decode to str first — same as the slow path (utf-8, errors=replace).
    # This prevents lxml's charset auto-detection from misreading <meta charset>
    # and double-encoding non-ASCII text.
    _original_bytes = html if isinstance(html, bytes) else html.encode('utf-8', errors='replace')
    if isinstance(html, bytes):
        html = html.decode('utf-8', errors='replace')

    nodes = []

    # DOCTYPE: read from raw string before passing to lxml
    stripped = html.lstrip()
    if stripped[:9].lower() == '<!doctype':
        end = stripped.find('>')
        if end > 0:
            decl = stripped[2:end].strip()
            nodes.append((T_DOCTYPE, None, decl))

    try:
        doc = _lxml_html.document_fromstring(html)
    except Exception:
        # Fallback: XHTML documents with <?xml ... encoding="..."?>
        # preambles raise "Unicode strings with encoding declaration are
        # not supported" on the string entry point. Retry with the raw
        # bytes and recover=True, which handles XML declarations correctly
        # and produces the same HTML5-compliant tree lxml would give us
        # for a well-formed document. Falls back to None (→ _SlowTokenizer)
        # only if both paths fail.
        try:
            parser = _etree.HTMLParser(recover=True)
            doc = _etree.fromstring(_original_bytes, parser)
            if doc is None:
                return None
        except Exception:
            return None   # caller falls back to slow path

    def emit_text(text, raw):
        if not text:
            return
        if raw:
            if text.strip():
                nodes.append((T_RAWTEXT, None, text))
        else:
            if text.strip():
                nodes.append((T_TEXT, None, text))
            elif ' ' in text and '\n' not in text:
                nodes.append((T_TEXT, None, ' '))

    # Iterative tree walker. Replaces the original recursive `walk(el)`
    # to eliminate a C-stack ceiling on deeply-nested HTML documents.
    # The output sequence is byte-identical to the recursive version for
    # every non-pathological input (validated against the 150k Common
    # Crawl Path A corpus before landing). Uses an explicit work stack
    # of two item shapes:
    #
    #   ("visit", el)          — process this lxml element/comment/PI
    #   ("close", tag, tail)   — emit T_CLOSE for `tag`, then `tail` text
    #
    # For a non-void element we push the close marker first, then the
    # children in reverse, so the next pop starts the first child. The
    # close marker fires after all descendants are drained, matching the
    # post-order position of `T_CLOSE` in the recursive version.
    work = [("visit", doc)]
    while work:
        item = work.pop()
        if item[0] == "close":
            _, tag, tail = item
            nodes.append((T_CLOSE, tag, None))
            emit_text(tail, False)
            continue

        el = item[1]
        tag = el.tag

        # Comments
        if tag is _etree.Comment:
            nodes.append((T_COMMENT, None, el.text or ''))
            emit_text(el.tail, False)
            continue

        # PIs and other callables — skip, emit tail
        if not isinstance(tag, str):
            emit_text(el.tail, False)
            continue

        # Strip XML namespace prefix if present. Guard against tags
        # where '{' appears without a matching '}' — lxml occasionally
        # surfaces such tags from malformed documents, and the previous
        # split(..., 1)[1] raised IndexError. Fix found during C.3
        # integration regression testing on Common Crawl.
        brace = tag.find('}')
        if brace >= 0:
            tag = tag[brace + 1:]
        tag = tag.lower()

        attrs = list(el.attrib.items())
        is_void = tag in VOID_ELEMENTS
        is_raw  = tag in RAWTEXT_TAGS

        nodes.append((T_SELFCLOSE if is_void else T_OPEN, tag, attrs))
        emit_text(el.text, is_raw)

        if is_void:
            emit_text(el.tail, False)
        else:
            # Defer close+tail until after all children have been walked.
            work.append(("close", tag, el.tail))
            # Push children in reverse so they pop in document order.
            children = list(el)
            for i in range(len(children) - 1, -1, -1):
                work.append(("visit", children[i]))

    return nodes


# ---------------------------------------------------------------------------
# HTMLTokenizer: public interface — uses lxml when available, falls back
# ---------------------------------------------------------------------------

class HTMLTokenizer:
    """HTML tokenizer. Uses lxml (C parser) when available, HTMLParser otherwise.

    Usage unchanged:
        tok = HTMLTokenizer()
        tok.feed(html_str_or_bytes)
        nodes = tok.nodes
    """
    def __init__(self):
        self.nodes = []
        self._verified_mode = False
        self._verified_tok = None
        import os as _os
        _flag = _os.environ.get("STORETLE_VERIFIED", "")
        if _flag and _flag.lower() not in ("0", "false", "no", ""):
            try:
                # Standard pip-installed import. If the package isn't
                # installed, this raises ImportError and we fall back
                # to the native path with a warning. A developer-machine
                # fallback (adding the source tree to sys.path) is
                # handled by conftest.py in the test suite, not here.
                from storetle_verified import VerifiedHTMLTokenizer
                self._verified_tok = VerifiedHTMLTokenizer()
                self._verified_mode = True
            except Exception as _e:
                _strict = _os.environ.get("STORETLE_VERIFIED_STRICT", "")
                _msg = (
                    f"[storetle] STORETLE_VERIFIED=1 but verified tokenizer "
                    f"unavailable ({type(_e).__name__}: {_e}); "
                    f"falling back to native path. "
                    f"Run `pip install storetle-verified` to enable verified mode.\n"
                )
                _increment_init_failures()
                _log.warning(
                    "event=verified_init_failure pid=%d tid=%d error_type=%s message=%r",
                    _os.getpid(), _current_tid(), type(_e).__name__, str(_e),
                )
                if _strict and _strict.lower() not in ("0", "false", "no", ""):
                    raise RuntimeError(_msg.strip()) from _e
                import sys as _sys
                _sys.stderr.write(_msg)
                self._verified_mode = False
                self._verified_tok = None

    def feed(self, html):
        if self._verified_mode and self._verified_tok is not None:
            try:
                self._verified_tok.feed(html)
                self.nodes = list(self._verified_tok.nodes)
                _increment_successes()
                return
            except Exception as _e:
                import os as _os2
                import sys as _sys
                _increment_feed_failures()
                _log.warning(
                    "event=verified_feed_failure pid=%d tid=%d error_type=%s message=%r",
                    _os2.getpid(), _current_tid(), type(_e).__name__, str(_e),
                )
                _sys.stderr.write(
                    f"[storetle] WARN: verified feed() raised ({_e!r}); "
                    f"disabling verified path and falling back to native.\n"
                )
                self._verified_mode = False
                self._verified_tok = None
        if _LXML:
            result = _tokenize_lxml(html)
            if result is not None:
                self.nodes = result
                return
        # Fallback
        p = _SlowTokenizer()
        if isinstance(html, bytes):
            html = html.decode('utf-8', errors='replace')
        p.feed(html)
        self.nodes = p.nodes


# ---------------------------------------------------------------------------
# String table builder
# ---------------------------------------------------------------------------

def _collect_strings(nodes):
    """
    Find every string in the document that is NOT in the shared vocabulary
    and appears 2+ times. Singletons are written inline in the content stream.
    Sort by frequency so the most common get the lowest (cheapest) IDs.
    Returns (file_strings_list, file_string_to_id).
    """
    counter = Counter()

    def count(s):
        if s not in SHARED_STR_TO_ID:
            counter[s] += 1

    for node_type, tag, payload in nodes:
        if node_type in (T_OPEN, T_SELFCLOSE):
            if tag not in TAG_TO_ID:
                count(tag)
            for attr_name, attr_value in payload:
                attr_name = attr_name.lower()
                if attr_name not in ATTR_TO_ID:
                    count(attr_name)
                if attr_value is not None:
                    if attr_name == 'class':
                        # Count individual tokens so common ones enter the table
                        for token in attr_value.split():
                            count(token)
                    else:
                        count(attr_value)
        elif node_type == T_CLOSE:
            if tag not in TAG_TO_ID:
                count(tag)
        # T_TEXT, T_DOCTYPE, T_COMMENT, T_RAWTEXT payloads are always written
        # inline in the content stream — never put in the string table.

    strings = [s for s, _ in counter.most_common()]
    string_to_id = {s: i for i, s in enumerate(strings)}
    return strings, string_to_id


def _build_string_table_bytes(strings: list) -> bytes:
    """Serialize + zlib-compress the string table (used by the single-file encoder)."""
    raw = bytearray()
    raw += struct.pack('>I', len(strings))
    for s in strings:
        encoded = s.encode('utf-8')
        raw += struct.pack('>I', len(encoded))
        raw += encoded
    return zlib.compress(bytes(raw), level=9)


def _serialize_string_table(strings):
    """Serialize string table as raw bytes — NO compression.
    Chunk-level zstd handles compression; per-doc zlib would break cross-doc patterns."""
    buf = bytearray()
    buf += struct.pack('>I', len(strings))
    for s in strings:
        encoded = s.encode('utf-8')
        buf += struct.pack('>I', len(encoded))
        buf += encoded
    return bytes(buf)


def _deserialize_string_table(raw):
    """Deserialize string table from raw bytes produced by _serialize_string_table."""
    pos = 0
    count = struct.unpack_from('>I', raw, pos)[0]; pos += 4
    strings = []
    for _ in range(count):
        length = struct.unpack_from('>I', raw, pos)[0]; pos += 4
        strings.append(raw[pos:pos + length].decode('utf-8')); pos += length
    return strings


def _build_streams_class_split(nodes):
    """Build (struct_bytes, content_bytes) with class token splitting but NO per-file table.

    Used by the streaming pipeline. Overhead-free: no table bytes embedded per doc.
    Class splitting is a pure win — 'flex items-center gap-4' → 3 shared-vocab IDs
    instead of one 22-byte inline string.
    """
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


def _build_streams_full(nodes, file_str_to_id):
    """Build (struct_bytes, content_bytes) using the full encoder:
    - per-file string table lookups (not just shared vocab)
    - class attribute token splitting
    This is the high-quality path used by the streaming format."""

    def wref(buf, s):
        _write_string_ref(buf, s, file_str_to_id)

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
# Core encode function
# ---------------------------------------------------------------------------

def encode(html_text: str) -> bytes:
    tokenizer = HTMLTokenizer()
    tokenizer.feed(html_text)
    nodes = tokenizer.nodes

    file_strings, file_str_to_id = _collect_strings(nodes)

    def wref(buf, s):
        """Write string s into buf as ID or inline."""
        _write_string_ref(buf, s, file_str_to_id)

    struct_stream  = bytearray()
    content_stream = bytearray()

    for node_type, tag, payload in nodes:

        if node_type in (T_OPEN, T_SELFCLOSE):
            tag_id = TAG_TO_ID.get(tag, UNKNOWN_ID)
            struct_stream.append(node_type)
            struct_stream.append(tag_id)
            if tag_id == UNKNOWN_ID:
                wref(content_stream, tag)

            attrs = payload
            struct_stream.append(len(attrs))

            for attr_name, attr_value in attrs:
                attr_name = attr_name.lower()
                attr_id = ATTR_TO_ID.get(attr_name, UNKNOWN_ID)
                struct_stream.append(attr_id)
                if attr_id == UNKNOWN_ID:
                    wref(content_stream, attr_name)

                if attr_value is None:
                    _write_sid(content_stream, None)
                elif attr_name == 'class':
                    _write_class_list(content_stream, attr_value, lambda b, s: wref(b, s))
                else:
                    wref(content_stream, attr_value)

        elif node_type == T_CLOSE:
            # tag_id NOT written — decoder reconstructs tag from a push/pop stack.
            # For unknown tags the name was already written at T_OPEN; no repeat.
            struct_stream.append(T_CLOSE)

        elif node_type in (T_TEXT, T_DOCTYPE, T_COMMENT, T_RAWTEXT):
            struct_stream.append(node_type)
            _write_inline(content_stream, payload)   # always inline — never in string table

    compressed_strtable  = _build_string_table_bytes(file_strings)
    compressed_struct    = zlib.compress(bytes(struct_stream),  level=9)
    compressed_content   = zlib.compress(bytes(content_stream), level=9)

    header = bytearray()
    header += MAGIC
    header.append(VERSION)
    header += struct.pack('>I', len(nodes))
    header += struct.pack('>I', len(compressed_strtable))
    header += struct.pack('>I', len(compressed_struct))
    header += struct.pack('>I', len(compressed_content))

    return (bytes(header)
            + bytes(compressed_strtable)
            + bytes(compressed_struct)
            + bytes(compressed_content))


def encode_file(input_path: str, output_path: str) -> dict:
    with open(input_path, 'r', encoding='utf-8', errors='replace') as f:
        html_text = f.read()

    original_size = len(html_text.encode('utf-8'))
    cube_bytes    = encode(html_text)
    encoded_size  = len(cube_bytes)

    with open(output_path, 'wb') as f:
        f.write(cube_bytes)

    node_count = struct.unpack_from('>I', cube_bytes, 5)[0]

    return {
        'original_size': original_size,
        'encoded_size':  encoded_size,
        'savings_pct':   round((1 - encoded_size / original_size) * 100, 1),
        'node_count':    node_count,
    }
