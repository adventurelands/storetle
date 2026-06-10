# registry.py — named corpora: `storetle get wiki "Albert Einstein"`.
#
# A corpus registry (corpora.json) lives next to the hosted data, so new
# corpora appear without a new package release. Title→location maps are
# fetched once and cached under ~/.cache/storetle/.

import gzip
import json
import time
import urllib.request
from pathlib import Path

REGISTRY_URL = 'https://data.davisbrief.com/corpora.json'
CACHE_DIR = Path.home() / '.cache' / 'storetle'
_REGISTRY_TTL = 3600


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'storetle-cli'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_registry():
    """Fetch corpora.json, with a small on-disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / 'corpora.json'
    if cache.exists() and time.time() - cache.stat().st_mtime < _REGISTRY_TTL:
        return json.loads(cache.read_text())
    try:
        data = _fetch(REGISTRY_URL)
        cache.write_bytes(data)
        return json.loads(data)
    except Exception:
        if cache.exists():               # stale beats broken
            return json.loads(cache.read_text())
        raise


def _titles_path(corpus_name, entry):
    """Download (once) and cache the corpus title map."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / f'titles-{corpus_name}.tsv.gz'
    if not local.exists():
        url = entry['base'].rstrip('/') + '/' + entry['titles']
        print(f'[storetle] fetching title index for "{corpus_name}" '
              f'({url.rsplit("/",1)[-1]}, one-time)...')
        local.write_bytes(_fetch(url))
    return local


def _norm(s):
    """Normalize a lookup key: catalog records (MARC) carry trailing
    '/' ';' '.' ',' punctuation that nobody types."""
    return s.strip().rstrip('/;,.').strip().lower()


def _lookup_title(corpus_name, entry, title):
    """Resolve a title to (shard_no, doc_idx).

    Match priority: exact > case-insensitive > punctuation-normalized.
    """
    want = title.strip()
    want_ci = want.lower()
    want_norm = _norm(title)
    ci_hit = norm_hit = None
    with gzip.open(_titles_path(corpus_name, entry), 'rt') as f:
        for line in f:
            name, shard, idx = line.rstrip('\n').rsplit('\t', 2)
            if name == want:
                return int(shard), int(idx)
            if ci_hit is None and name.lower() == want_ci:
                ci_hit = (int(shard), int(idx))
            elif norm_hit is None and _norm(name) == want_norm:
                norm_hit = (int(shard), int(idx))
    hit = ci_hit or norm_hit
    if hit:
        return hit
    raise KeyError(f'title not found in corpus "{corpus_name}": {title!r} '
                   f'(try: storetle search {corpus_name} "<part of title>")')


def search_titles(corpus_name, query, limit=25):
    """Substring search over a corpus's lookup keys.

    Returns a list of (name, shard_no, doc_idx), at most `limit`.
    """
    reg = load_registry()
    if corpus_name not in reg:
        raise KeyError(f'unknown corpus {corpus_name!r}; '
                       f'available: {", ".join(sorted(reg))}')
    entry = reg[corpus_name]
    q = _norm(query)
    hits = []
    with gzip.open(_titles_path(corpus_name, entry), 'rt') as f:
        for line in f:
            name, shard, idx = line.rstrip('\n').rsplit('\t', 2)
            if q in name.lower():
                hits.append((name, int(shard), int(idx)))
                if len(hits) >= limit:
                    break
    return hits


def resolve(corpus_name, ref):
    """Resolve (corpus, index-or-title) → (shard_url, doc_idx).

    ref may be an integer global index or a document title.
    """
    reg = load_registry()
    if corpus_name not in reg:
        raise KeyError(f'unknown corpus {corpus_name!r}; '
                       f'available: {", ".join(sorted(reg))}')
    entry = reg[corpus_name]
    base = entry['base'].rstrip('/')
    shards = entry['shards']

    try:
        gidx = int(ref)
    except (TypeError, ValueError):
        shard_no, idx = _lookup_title(corpus_name, entry, ref)
        return f'{base}/{shards[shard_no]}', idx

    # integer: map global index onto shards via per-shard doc counts
    counts = entry.get('shard_docs') or []
    if not counts:
        return f'{base}/{shards[0]}', gidx
    run = 0
    for shard_no, n in enumerate(counts):
        if gidx < run + n:
            return f'{base}/{shards[shard_no]}', gidx - run
        run += n
    raise IndexError(f'index {gidx} out of range ({run} docs in corpus)')


def list_corpora():
    reg = load_registry()
    return {name: {k: v for k, v in e.items() if k in
                   ('title', 'docs', 'snapshot', 'license')}
            for name, e in sorted(reg.items())}
