# receipt.py — verifiable, Bitcoin-anchored receipts for streamed corpora.
#
# When you stream a corpus with `--receipt`, the wheel commits to exactly the
# bytes it served (a Merkle root over per-doc SHA-256s), hands that root to the
# storetle verified API, and the API:
#   1. appends it to a signed, append-only transparency log,
#   2. anchors the signed tree head into Bitcoin via OpenTimestamps,
#   3. returns a receipt you can verify offline with `ots verify`.
#
# The receipt proves: "this exact set of documents, in this order, committed to
# this root, was served at this time and anchored in Bitcoin block N." Nobody
# (including storetle) can backdate or alter it after the fact.
#
# Stdlib only — no third-party deps in the wheel.

import hashlib
import json
import os
import time
import urllib.request
import urllib.error

DEFAULT_API = os.environ.get("STORETLE_API", "https://storetle.davisbrief.com")
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
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_bytes(url, api_key=None, timeout=30):
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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
