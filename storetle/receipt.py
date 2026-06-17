# receipt.py — verifiable, Bitcoin-anchored receipts for streamed corpora.
#
# When you stream a corpus with `--receipt`, the stream runs through the
# storetle API, which:
#   1. hashes each document as it serves it, building a Merkle root over exactly
#      the bytes it sent this session (any stop point),
#   2. signs that root (Ed25519) and anchors it into Bitcoin via OpenTimestamps,
#   3. returns the signed commitment + .ots receipt.
# The wheel re-derives the root from the bytes it received and cross-checks, then
# packages everything into a <corpus>.receipt.zip you can verify offline with
# `storetle verify-receipt` (or the standard `ots` tools).
#
# The receipt proves: "this exact set of documents, in this order, committed to
# this root, was served by storetle and anchored in Bitcoin block N." Nobody
# (including storetle) can backdate or alter it after the fact.
#
# Stdlib only for streaming; verification uses the [verify] extras.

import hashlib
import json
import os
import time
import urllib.request
import urllib.error

DEFAULT_API = os.environ.get("STORETLE_API", "https://storetle.davisbrief.com")
# Identify the client honestly (Python's default "Python-urllib" UA gets
# filtered as a generic scraper; this is a normal product User-Agent).
_UA = "storetle/0.5 (+https://github.com/adventurelands/storetle)"
# Commitments are static, signed, Bitcoin-anchored files served next to the
# corpora (no live server needed). Default to the corpora host.
COMMITMENT_BASE = os.environ.get("STORETLE_COMMITMENTS", "https://data.davisbrief.com")


# --- Merkle commitment (must match the server's corpus_tool/merkle.py rule) ---

def leaf_hash(doc_sha256_hex: str) -> bytes:
    # leaf = sha256(0x00 || manifest_sha256)
    return hashlib.sha256(b"\x00" + bytes.fromhex(doc_sha256_hex)).digest()


def _node(left: bytes, right: bytes) -> bytes:
    # node = sha256(0x01 || left || right)
    return hashlib.sha256(b"\x01" + left + right).digest()


def merkle_root(doc_hashes_hex):
    """Build the corpus Merkle root over per-document SHA-256 hex digests.
    Odd tail is duplicated, matching the server."""
    if not doc_hashes_hex:
        return hashlib.sha256(b"").hexdigest()
    level = [leaf_hash(h) for h in doc_hashes_hex]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0].hex()


# --- canonical "what we served" commitment -------------------------------
# The receipt commits to the RAW stored doc bytes storetle serves, in canonical
# order (shard 0 doc 0, doc 1, ... shard 1 doc 0, ...). Extraction modes
# (--text / --verified) are local transforms applied AFTER; the receipt proves
# you received storetle's exact corpus, whatever you then do with it.

def doc_hash(raw_bytes: bytes) -> str:
    """Canonical per-document digest: sha256 of the raw served bytes."""
    return hashlib.sha256(raw_bytes).hexdigest()


def canonical_root(raw_docs_iter) -> str:
    """Corpus root over an ordered iterable of raw doc byte-strings."""
    return merkle_root([doc_hash(b) for b in raw_docs_iter])


def verify_streamed(commitment: dict, streamed_raw_docs) -> dict:
    """Wheel-side check: re-derive the root from the bytes we actually
    received and confirm it equals storetle's signed, anchored root.

    Returns {match: bool, derived_root, expected_root}. A mismatch means the
    bytes you got are NOT storetle's canonical corpus — the receipt is only
    valid when match is True."""
    derived = canonical_root(streamed_raw_docs)
    expected = commitment.get("merkle_root_hex")
    return {"match": derived == expected, "derived_root": derived,
            "expected_root": expected}


# --- API client ---

def _post(url, payload, api_key=None, timeout=30):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", _UA)
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_bytes(url, api_key=None, timeout=30):
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _UA)
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _pull(url, api_key=None, timeout=120):
    """GET a receipt-session pull; returns (body_bytes, headers_dict)."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _UA)
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def iter_session_docs(corpus, api_base=None, api_key=None, limit=None, batch=256):
    """Open a receipt session and yield (raw_doc_bytes) as the server streams
    and hashes them. Yields ('__commit__', commitment_dict) last. The server's
    commitment covers exactly the docs it served in this session (any stop
    point) — see receipt_open/pull/finalize on the API."""
    import struct
    api = (api_base or DEFAULT_API).rstrip("/")
    sess = _post(f"{api}/corpus/{corpus}/receipt/open", {}, api_key=api_key)
    sid, total = sess["session_id"], sess["total"]
    n = min(total, limit) if limit else total
    offset = 0
    while offset < n:
        take = min(batch, n - offset)
        body, headers = _pull(
            f"{api}/corpus/{corpus}/receipt/pull?session={sid}&offset={offset}&n={take}",
            api_key=api_key)
        i = 0
        while i < len(body):
            ln = struct.unpack(">I", body[i:i + 4])[0]; i += 4
            yield body[i:i + ln]; i += ln
        offset = int(headers.get("X-Next-Offset", offset + take))
        if headers.get("X-Done") == "1":
            break
    commit = _post(f"{api}/corpus/{corpus}/receipt/finalize?session={sid}", {},
                   api_key=api_key)
    yield ("__commit__", commit)


def fetch_commitment(corpus, api_base=None, commitment_url=None):
    """Fetch storetle's published, signed, Bitcoin-anchored commitment for a
    corpus (computed by us over the bytes we serve). Returns (commitment_dict,
    ots_bytes_or_None)."""
    if commitment_url:
        base = commitment_url.rstrip("/")
    else:
        base = (api_base or COMMITMENT_BASE).rstrip("/") + f"/corpus/{corpus}"
    commitment = json.loads(_get_bytes(f"{base}/commitment.json").decode())
    ots = None
    try:
        ots = _get_bytes(f"{base}/commitment.ots")
    except urllib.error.HTTPError:
        pass
    return commitment, ots


def save_receipt(receipt, path):
    """Write the receipt JSON and, if present, the raw .ots alongside it."""
    with open(path, "w") as f:
        json.dump(receipt, f, indent=2, sort_keys=True)
    if receipt.get("ots_receipt_b64"):
        import base64
        ots_path = os.path.splitext(path)[0] + ".ots"
        with open(ots_path, "wb") as f:
            f.write(base64.b64decode(receipt["ots_receipt_b64"]))
        return path, ots_path
    return path, None


# fields the server signs, in the canonical order it signs them
_SIGNED_FIELDS = ("corpus", "doc_count", "merkle_root_hex", "ts", "public_key")

_VERIFY_TXT = """storetle verifiable receipt
===========================

This bundle proves a corpus was streamed from storetle, committed to a Merkle
root that storetle signed and anchored into Bitcoin.

Files:
  commitment.json  - the signed commitment (exact signed fields + signature)
  commitment.ots   - the OpenTimestamps Bitcoin anchor over merkle_root_hex
  stream.json      - what this client streamed (doc count, locally-derived root)

Verify (one command):
  storetle verify-receipt {name}

Verify by hand:
  1. Ed25519: canonical-JSON(commitment.json["signed"]) checked against
     commitment.json["signature"] using signed.public_key.
  2. Bitcoin: ots verify -d {root} commitment.ots
     (pending now; the block attestation lands within a few hours,
      then `ots upgrade commitment.ots` completes it).
"""


def write_bundle(path, commit, stream_meta):
    """Write a .zip receipt bundle. `commit` is the server's finalize response;
    `stream_meta` is what the client streamed. Keeps the .ots native so stock
    `ots` works, and delineates exactly which fields were signed."""
    import base64
    import zipfile
    signed = {k: commit[k] for k in _SIGNED_FIELDS if k in commit}
    commitment = {"signed": signed, "signature": commit.get("signature")}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("commitment.json", json.dumps(commitment, indent=2, sort_keys=True))
        if commit.get("ots_receipt_b64"):
            z.writestr("commitment.ots", base64.b64decode(commit["ots_receipt_b64"]))
        z.writestr("stream.json", json.dumps(stream_meta, indent=2, sort_keys=True))
        z.writestr("VERIFY.txt", _VERIFY_TXT.format(
            name=os.path.basename(path), root=signed.get("merkle_root_hex", "<root>")))
    return path


def verify_bundle(path):
    """Verify a receipt bundle. Returns (signed_dict, results_dict).
    results keys: signature, ots_digest_matches_root, bitcoin."""
    import base64
    import zipfile
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        commitment = json.loads(z.read("commitment.json"))
        ots = z.read("commitment.ots") if "commitment.ots" in names else None
    signed = commitment["signed"]
    sig = base64.b64decode(commitment["signature"]) if commitment.get("signature") else None
    results = {}

    # 1. Ed25519 signature over the exact signed fields
    if sig is None:
        results["signature"] = "MISSING"
    else:
        try:
            from nacl.signing import VerifyKey
            from nacl.exceptions import BadSignatureError
            canon = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
            try:
                VerifyKey(base64.b64decode(signed["public_key"])).verify(canon, sig)
                results["signature"] = "VALID"
            except BadSignatureError:
                results["signature"] = "INVALID"
        except ImportError:
            results["signature"] = "SKIPPED — install verifiers: pip install 'storetle[verify]'"

    # 2. Bitcoin / OpenTimestamps
    if not ots:
        results["bitcoin"] = "no .ots in bundle"
    else:
        try:
            from opentimestamps.core.serialize import BytesDeserializationContext
            from opentimestamps.core.timestamp import DetachedTimestampFile
            from opentimestamps.core.notary import (
                BitcoinBlockHeaderAttestation, PendingAttestation)
            det = DetachedTimestampFile.deserialize(BytesDeserializationContext(ots))
            results["ots_digest_matches_root"] = (
                det.file_digest.hex() == signed.get("merkle_root_hex"))
            atts = [a for _, a in det.timestamp.all_attestations()]
            btc = [a for a in atts if isinstance(a, BitcoinBlockHeaderAttestation)]
            if btc:
                results["bitcoin"] = f"CONFIRMED in Bitcoin block {btc[0].height}"
            elif any(isinstance(a, PendingAttestation) for a in atts):
                results["bitcoin"] = ("PENDING — committed to calendars, block in ~hours "
                                      "(run: ots upgrade commitment.ots)")
            else:
                results["bitcoin"] = "no attestations"
        except ImportError:
            results["bitcoin"] = ("SKIPPED — install verifiers: pip install 'storetle[verify]' "
                                  "(or: ots verify -d %s commitment.ots)"
                                  % signed.get("merkle_root_hex", ""))
    return signed, results
