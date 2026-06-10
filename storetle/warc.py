# warc.py — WARC read/write for storetle
#
# Handles .warc (uncompressed) and .warc.gz (per-record gzip, Common Crawl format).
#
# Public API:
#   iter_warc_html(path)              → yields (url, html_bytes) for HTML response records
#   from_warc(warc_path, out_path)    → convert WARC → .storetle
#   to_warc(storetle_path, out_path)  → convert .storetle → WARC
#   warc_encode(input, output)        → encode HTML records in-place → valid .warc.gz
#   warc_decode(input, output)        → decode storetle-encoded WARC → standard WARC

import gzip
import struct
from pathlib import Path


# ---------------------------------------------------------------------------
# WARC record parsing
# ---------------------------------------------------------------------------

def _open_warc(path):
    """Open a WARC file for reading, handling both .warc and .warc.gz."""
    path = str(path)
    if path.endswith('.gz'):
        # Python's gzip module transparently handles concatenated gzip streams
        # (each WARC record is its own gzip frame in Common Crawl format)
        return gzip.open(path, 'rb')
    return open(path, 'rb')


def _parse_records(fh):
    """Parse a stream of WARC records. Yields (warc_headers, payload_bytes)."""
    while True:
        # Scan for WARC record start
        line = fh.readline()
        if not line:
            break
        if not line.startswith(b'WARC/1.'):
            continue

        # Read WARC headers
        warc_headers = {}
        while True:
            hline = fh.readline()
            if hline in (b'\r\n', b'\n', b''):
                break
            if b':' in hline:
                k, _, v = hline.partition(b':')
                key = k.strip().lower().decode('utf-8', errors='replace')
                val = v.strip().decode('utf-8', errors='replace')
                warc_headers[key] = val

        # Read payload
        try:
            clen = int(warc_headers.get('content-length', '0'))
        except ValueError:
            clen = 0

        payload = fh.read(clen) if clen > 0 else b''
        # Skip trailing CRLFCRLF between records
        fh.read(4)

        yield warc_headers, payload


def _split_http_response(payload):
    """Split HTTP response payload into (http_headers_dict, body_bytes)."""
    # Find blank line separating HTTP headers from body
    sep = payload.find(b'\r\n\r\n')
    if sep == -1:
        sep = payload.find(b'\n\n')
        if sep == -1:
            return {}, payload
        head_raw = payload[:sep]
        body = payload[sep + 2:]
    else:
        head_raw = payload[:sep]
        body = payload[sep + 4:]

    http_headers = {}
    for line in head_raw.split(b'\n')[1:]:   # skip status line
        line = line.rstrip(b'\r')
        if b':' in line:
            k, _, v = line.partition(b':')
            key = k.strip().lower().decode('utf-8', errors='replace')
            val = v.strip().decode('utf-8', errors='replace')
            http_headers[key] = val

    return http_headers, body


def iter_warc_html(path):
    """Yield (url, html_bytes) for every HTML response record in a WARC file.

    Handles .warc and .warc.gz. Skips non-HTML records automatically.

    Example:
        for url, html in iter_warc_html('crawl.warc.gz'):
            process(url, html)
    """
    fh = _open_warc(path)
    try:
        for warc_headers, payload in _parse_records(fh):
            if warc_headers.get('warc-type', '').lower() != 'response':
                continue
            url = warc_headers.get('warc-target-uri', '')
            http_headers, body = _split_http_response(payload)
            content_type = http_headers.get('content-type', '')
            # Accept text/html explicitly, or when Content-Type is absent/ambiguous
            if body and ('text/html' in content_type or not content_type):
                # Quick sanity check: looks like HTML?
                snippet = body.lstrip()[:20].lower()
                if not content_type and not (snippet.startswith(b'<!') or snippet.startswith(b'<h')):
                    continue
                yield url, body
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# WARC writing
# ---------------------------------------------------------------------------

def _gzip_frame(data):
    """Compress data as a single gzip frame (for per-record WARC.gz)."""
    import io
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def _build_warc_record(url, html_bytes, date_str, record_id):
    """Build a single WARC response record as raw bytes."""
    http_resp = (
        b'HTTP/1.1 200 OK\r\n'
        b'Content-Type: text/html; charset=utf-8\r\n'
        + ('Content-Length: %d\r\n' % len(html_bytes)).encode()
        + b'\r\n'
        + html_bytes
    )
    warc_hdr = (
        b'WARC/1.0\r\n'
        b'WARC-Type: response\r\n'
        + ('WARC-Target-URI: %s\r\n' % url).encode()
        + ('WARC-Date: %s\r\n' % date_str).encode()
        + ('WARC-Record-ID: <urn:uuid:storetle-%08d>\r\n' % record_id).encode()
        + ('Content-Length: %d\r\n' % len(http_resp)).encode()
        + b'Content-Type: application/http; msgtype=response\r\n'
        + b'\r\n'
    )
    return warc_hdr + http_resp + b'\r\n\r\n'


# ---------------------------------------------------------------------------
# High-level converters
# ---------------------------------------------------------------------------

def from_warc(warc_path, out_path, verbose=True):
    """Convert a WARC file → .storetle archive.

    Extracts HTML response bodies. URL metadata is not stored in v0.1
    (the format stores raw HTML blobs only).

    Returns dict with doc count and size stats.
    """
    from .stream import StreamWriter, StreamReader
    import os

    warc_path = Path(warc_path)
    out_path  = str(out_path)

    n = 0
    skipped = 0
    with StreamWriter(out_path) as w:
        for url, html in iter_warc_html(warc_path):
            w.append(html)
            n += 1
            if verbose and n % 1000 == 0:
                print('  %d HTML records encoded...' % n)

    if n == 0:
        import os
        if os.path.exists(out_path):
            os.unlink(out_path)
        raise ValueError('No HTML records found in %s' % warc_path)

    info = StreamReader.info(out_path)
    if verbose:
        def fmt(x):
            if x < 1048576: return '%.1fKB' % (x / 1024)
            return '%.2fMB' % (x / 1048576)
        print('Done: %d HTML docs  %s → %s  (%.1f%% saved)' % (
            n, fmt(info['original_bytes']), fmt(info['compressed_bytes']),
            info['ratio_pct']))
        print('Output: %s' % out_path)

    return info


def to_warc(storetle_path, out_path, compress=True, verbose=True):
    """Convert a .storetle archive → WARC file.

    Each document becomes a WARC response record with a synthetic URI
    <urn:storetle:N>. Output is .warc (uncompressed) or .warc.gz
    (per-record gzip, Common Crawl compatible) based on file extension.
    """
    from .stream import StreamReader

    storetle_path = str(storetle_path)
    out_path      = Path(out_path)
    per_record_gz = str(out_path).endswith('.gz')

    try:
        from datetime import datetime
        date_str = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        date_str = '2026-01-01T00:00:00Z'

    n = 0
    with open(str(out_path), 'wb') as fh:
        with StreamReader(storetle_path) as r:
            total = r.doc_count
            for i, html in enumerate(r):
                url    = '<urn:storetle:%d>' % i
                record = _build_warc_record(url, html, date_str, i)
                if per_record_gz:
                    fh.write(_gzip_frame(record))
                else:
                    fh.write(record)
                n += 1
                if verbose and n % 1000 == 0:
                    print('  %d/%d records written...' % (n, total))

    if verbose:
        import os
        out_size = os.path.getsize(str(out_path))
        def fmt(x):
            if x < 1048576: return '%.1fKB' % (x / 1024)
            return '%.2fMB' % (x / 1048576)
        print('Done: %d docs → %s  (%s)' % (n, out_path, fmt(out_size)))

    return n


# ---------------------------------------------------------------------------
# In-place WARC encoding/decoding — valid .warc.gz in, valid .warc.gz out
# ---------------------------------------------------------------------------

_STORETLE_VERSION_HEADER = b'WARC-Storetle-Version: 2\r\n'
_STORETLE_VERSION_KEY    = 'warc-storetle-version'


def _rebuild_warc_record(original_headers_raw, http_status_line,
                          http_headers_raw, new_body, encode=True):
    """Rebuild a WARC response record with a new body.

    Preserves all original WARC headers except Content-Length (updated).
    Adds/removes WARC-Storetle-Version marker.
    Returns raw record bytes (uncompressed).
    """
    new_ct    = b'Content-Type: application/x-storetle-html\r\n' if encode \
                else b'Content-Type: text/html; charset=utf-8\r\n'
    new_http  = (
        http_status_line + b'\r\n'
        + new_ct
        + ('Content-Length: %d\r\n' % len(new_body)).encode()
        + b'\r\n'
        + new_body
    )

    # Rebuild WARC headers: strip old Content-Length, add/remove storetle marker
    warc_lines = []
    for line in original_headers_raw.split(b'\r\n'):
        if not line:
            continue
        low = line.lower()
        if low.startswith(b'content-length:'):
            continue
        if low.startswith(b'warc-storetle-version:'):
            continue
        warc_lines.append(line)

    warc_block = b'\r\n'.join(warc_lines) + b'\r\n'
    if encode:
        warc_block += _STORETLE_VERSION_HEADER
    warc_block += ('Content-Length: %d\r\n' % len(new_http)).encode()
    warc_block += b'\r\n'

    return warc_block + new_http + b'\r\n\r\n'


def _iter_raw_records(fh):
    """Yield raw (warc_header_bytes, payload_bytes, is_response, is_html, is_storetle) tuples.

    warc_header_bytes: everything from WARC/1.0 up to but not including the blank line.
    payload_bytes: Content-Length bytes after the blank line.
    """
    while True:
        line = fh.readline()
        if not line:
            break
        if not line.startswith(b'WARC/1.'):
            continue

        # Collect raw header lines
        header_lines = [line.rstrip(b'\r\n')]
        warc_meta = {}
        while True:
            hline = fh.readline()
            if hline in (b'\r\n', b'\n', b''):
                break
            header_lines.append(hline.rstrip(b'\r\n'))
            if b':' in hline:
                k, _, v = hline.partition(b':')
                warc_meta[k.strip().lower().decode('utf-8', errors='replace')] = \
                    v.strip().decode('utf-8', errors='replace')

        try:
            clen = int(warc_meta.get('content-length', '0'))
        except ValueError:
            clen = 0

        payload = fh.read(clen) if clen > 0 else b''
        fh.read(4)  # skip CRLFCRLF

        warc_type   = warc_meta.get('warc-type', '').lower()
        is_response = warc_type == 'response'
        is_storetle = _STORETLE_VERSION_KEY in warc_meta

        # Check if payload is an HTML HTTP response
        is_html = False
        if is_response and payload:
            _, http_headers, _ = _split_http_response_raw(payload)
            ct = http_headers.get('content-type', '')
            is_html = 'text/html' in ct or 'x-storetle-html' in ct

        yield b'\r\n'.join(header_lines), payload, is_response, is_html, is_storetle


def _split_http_response_raw(payload):
    """Split HTTP response into (status_line_bytes, headers_dict, body_bytes)."""
    sep = payload.find(b'\r\n\r\n')
    if sep == -1:
        sep = payload.find(b'\n\n')
        if sep == -1:
            return b'HTTP/1.1 200 OK', {}, payload
        head = payload[:sep]; body = payload[sep+2:]
        nl = b'\n'
    else:
        head = payload[:sep]; body = payload[sep+4:]
        nl = b'\r\n'

    lines = head.split(nl)
    status_line = lines[0] if lines else b'HTTP/1.1 200 OK'
    headers = {}
    for ln in lines[1:]:
        ln = ln.rstrip(b'\r')
        if b':' in ln:
            k, _, v = ln.partition(b':')
            headers[k.strip().lower().decode('utf-8', errors='replace')] = \
                v.strip().decode('utf-8', errors='replace')
    return status_line, headers, body


def warc_encode(input_path, output_path, verbose=True):
    """Encode HTML records in a WARC file using Storetle compression.

    Output is a valid .warc.gz file — every existing WARC tool keeps working.
    HTML response records are encoded with Storetle and marked with
    WARC-Storetle-Version: 2. All other records pass through unchanged.

    To decode back to standard HTML WARC: warc_decode(output, restored)
    """
    from .stream import _encode_doc, _decode_doc

    input_path  = str(input_path)
    output_path = str(output_path)
    per_rec_gz  = output_path.endswith('.gz')

    fh_in = _open_warc(input_path)
    fh_out = open(output_path, 'wb')

    n_encoded = n_passthrough = 0
    bytes_html_orig = bytes_encoded = 0

    try:
        for warc_hdr_raw, payload, is_response, is_html, is_storetle in \
                _iter_raw_records(fh_in):

            if is_response and is_html and not is_storetle:
                status_line, http_headers, body = _split_http_response_raw(payload)
                try:
                    encoded = _encode_doc(body)
                    record  = _rebuild_warc_record(
                        warc_hdr_raw, status_line, http_headers, encoded, encode=True)
                    bytes_html_orig += len(body)
                    bytes_encoded   += len(encoded)
                    n_encoded += 1
                except Exception:
                    # encoding failed — pass through unchanged
                    record = warc_hdr_raw + b'\r\n\r\n' + payload + b'\r\n\r\n'
                    n_passthrough += 1
            else:
                # Non-HTML, non-response, or already encoded — pass through
                record = warc_hdr_raw + b'\r\n\r\n' + payload + b'\r\n\r\n'
                n_passthrough += 1

            fh_out.write(_gzip_frame(record) if per_rec_gz else record)

    finally:
        fh_in.close()
        fh_out.close()

    import os
    out_size = os.path.getsize(output_path)

    if verbose:
        def fmt(x):
            if x < 1048576: return '%.1fKB' % (x / 1024)
            return '%.2fMB' % (x / 1048576)
        print('warc-encode: %d HTML records encoded, %d passed through' % (
            n_encoded, n_passthrough))
        print('  HTML original:  %s  →  encoded: %s  (%.1f%% smaller pre-gzip)' % (
            fmt(bytes_html_orig), fmt(bytes_encoded),
            100 * (1 - bytes_encoded / max(bytes_html_orig, 1))))
        print('  Output file:    %s  (%s)' % (output_path, fmt(out_size)))

    return {'encoded': n_encoded, 'passthrough': n_passthrough,
            'output_bytes': out_size, 'html_orig': bytes_html_orig}


def warc_decode(input_path, output_path, verbose=True):
    """Decode a Storetle-encoded WARC back to standard HTML WARC.

    Finds records marked with WARC-Storetle-Version, decodes the HTML,
    and writes a standard WARC that any tool can read without Storetle.
    """
    from .stream import _decode_doc

    input_path  = str(input_path)
    output_path = str(output_path)
    per_rec_gz  = output_path.endswith('.gz')

    fh_in  = _open_warc(input_path)
    fh_out = open(output_path, 'wb')

    n_decoded = n_passthrough = 0

    try:
        for warc_hdr_raw, payload, is_response, is_html, is_storetle in \
                _iter_raw_records(fh_in):

            if is_storetle and is_response:
                status_line, http_headers, body = _split_http_response_raw(payload)
                try:
                    html    = _decode_doc(body)
                    record  = _rebuild_warc_record(
                        warc_hdr_raw, status_line, http_headers, html, encode=False)
                    n_decoded += 1
                except Exception:
                    record = warc_hdr_raw + b'\r\n\r\n' + payload + b'\r\n\r\n'
                    n_passthrough += 1
            else:
                record = warc_hdr_raw + b'\r\n\r\n' + payload + b'\r\n\r\n'
                n_passthrough += 1

            fh_out.write(_gzip_frame(record) if per_rec_gz else record)

    finally:
        fh_in.close()
        fh_out.close()

    if verbose:
        print('warc-decode: %d records decoded, %d passed through' % (
            n_decoded, n_passthrough))

    return {'decoded': n_decoded, 'passthrough': n_passthrough}
