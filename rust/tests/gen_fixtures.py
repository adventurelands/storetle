#!/usr/bin/env python3.11
"""gen_fixtures.py — one-time generator for the Rust differential test fixtures.

Written against the PRE-PACKAGE FLAT LAYOUT of the repo (stream.py at the repo
root, imported as `from stream import StreamWriter`). The Python code is being
restructured into a storetle/ package; if you need to regenerate after that,
update the import below. The generated fixtures under tests/fixtures/ are
CHECKED IN — `cargo test` never runs Python.

Generates:
  fixtures/basic.storetle        12 varied docs, external dictionary
  fixtures/multi.storetle        300 templated docs (2 chunks), external dict
  fixtures/embedded.storetle     5 docs, dictionary embedded in the file
  fixtures/cube_dict_v10.bin     copy of the codec dictionary
  fixtures/manifest.txt          doc/chunk counts the Rust test asserts on
  fixtures/expected/*.html       Python-decoded HTML (ground truth, byte-exact)

Run with: python3.11 tests/gen_fixtures.py
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # storetle repo root (flat layout)
sys.path.insert(0, str(REPO))

from storetle.stream import StreamReader, StreamWriter  # noqa: E402
from storetle import zstd_compat as _zs  # noqa: E402

assert _zs.available(), "zstd not available to Python — fixtures would be brotli"

FIX = HERE / "fixtures"
EXP = FIX / "expected"
FIX.mkdir(parents=True, exist_ok=True)
EXP.mkdir(parents=True, exist_ok=True)

DICT_PATH = REPO / "storetle" / "cube_dict_v10.bin"

# ---------------------------------------------------------------------------
# Hand-written fixture documents
# ---------------------------------------------------------------------------

BASIC_DOCS = [
    # 0: minimal
    "<html><head><title>Hello</title></head><body><p>world</p></body></html>",
    # 1: doctype + nested tags + comment
    """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Nested</title></head>
<body>
  <!-- a comment -->
  <div><section><article><h1>Deep</h1><p>nesting <em>works</em> fine</p></article></section></div>
</body>
</html>""",
    # 2: unicode text and attributes
    """<html><body>
<h1>Unicode: héllo wörld — 日本語テキスト 🦀</h1>
<p title="naïve café">Ω≈ç√∫˜µ≤≥÷ and emoji 🎉🚀</p>
<p>Кириллица, العربية, עברית</p>
</body></html>""",
    # 3: class attributes (shared-vocab tokens + unknown tokens)
    """<html><body>
<div class="flex items-center gap-4">
  <span class="btn btn-primary my-very-custom-class">a</span>
  <span class="singletoken">b</span>
</div>
</body></html>""",
    # 4: unknown tags and unknown attributes
    """<html><body>
<my-widget data-custom-thing="42" weirdattr="x">
  <another-unknown-tag>inside</another-unknown-tag>
</my-widget>
</body></html>""",
    # 5: void elements + boolean attributes
    """<html><body>
<form action="/s" method="get">
<input type="text" name="q" required disabled>
<br>
<img src="a.png" alt="pic">
<hr>
</form>
</body></html>""",
    # 6: script/style raw text (no escaping on decode)
    """<html><head>
<style>body { color: #fff; } a > b { x: "y" }</style>
<script>if (a < b && c > d) { alert("hi & bye"); }</script>
</head><body><p>after</p></body></html>""",
    # 7: text needing entity escaping
    "<html><body><p>5 &lt; 6 &amp;&amp; 7 &gt; 3, \"quotes\" 'single'</p></body></html>",
    # 8: attribute values needing escaping
    '<html><body><a href="/x?a=1&amp;b=2" title="say &quot;hi&quot; &lt;now&gt;">link</a></body></html>',
    # 9: long inline text (> 254 bytes — exercises the 0xFF length escape)
    "<html><body><p>" + ("long-unique-token-xyzzy " * 20) + "</p></body></html>",
    # 10: empty-ish doc
    "<html></html>",
    # 11: table + svg attrs
    """<html><body>
<table><thead><tr><th colspan="2">h</th></tr></thead>
<tbody><tr><td>1</td><td>2</td></tr></tbody></table>
<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="red"></circle></svg>
</body></html>""",
]

EMBEDDED_DOCS = [
    "<html><body><h1>embedded dict sample</h1><p>doc zero</p></body></html>",
    """<!DOCTYPE html><html><body>
<div class="flex items-center"><p>Unicode in embedded: ünïcødé 中文 ✓</p></div>
</body></html>""",
    "<html><body><custom-el attr-x=\"1\"><p>unknown tag</p></custom-el></body></html>",
    "<html><head><script>let x = 1 < 2;</script></head><body><!-- c --></body></html>",
    "<html><body><p>last &amp; final &lt;doc&gt;</p></body></html>",
]


def multi_doc(i: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><title>Doc {i}</title></head>
<body class="flex items-center page-{i}">
  <h1>Document number {i}</h1>
  <p>Some shared boilerplate text appears in every page so the dictionary helps.</p>
  <p>Unique bit: token-{i}-αβγ-{i * 7919}</p>
</body>
</html>"""


def write_archive(path, docs, embed_dict=False):
    w = StreamWriter(path, embed_dict=embed_dict)
    for d in docs:
        w.append(d)
    w.close()
    return w.stats()


def dump_expected(archive_path, prefix, indices=None):
    with StreamReader(archive_path) as r:
        idxs = range(r.doc_count) if indices is None else indices
        for i in idxs:
            (EXP / f"{prefix}_{i:06}.html").write_bytes(r.get(i))
        return r.doc_count, len(r._index)


def main():
    shutil.copyfile(DICT_PATH, FIX / "cube_dict_v10.bin")

    s1 = write_archive(FIX / "basic.storetle", BASIC_DOCS)
    n1, c1 = dump_expected(FIX / "basic.storetle", "basic")

    multi_docs = [multi_doc(i) for i in range(300)]
    s2 = write_archive(FIX / "multi.storetle", multi_docs)
    # Only spot-check expected outputs for multi (incl. chunk boundaries)
    multi_samples = [0, 1, 127, 255, 256, 298, 299]
    n2, c2 = dump_expected(FIX / "multi.storetle", "multi", multi_samples)

    s3 = write_archive(FIX / "embedded.storetle", EMBEDDED_DOCS, embed_dict=True)
    n3, c3 = dump_expected(FIX / "embedded.storetle", "embedded")

    manifest = [
        f"basic.doc_count={n1}",
        f"basic.chunk_count={c1}",
        f"multi.doc_count={n2}",
        f"multi.chunk_count={c2}",
        f"multi.samples={','.join(map(str, multi_samples))}",
        f"embedded.doc_count={n3}",
        f"embedded.chunk_count={c3}",
        "",
    ]
    (FIX / "manifest.txt").write_text("\n".join(manifest))

    print("basic   :", s1, f"chunks={c1}")
    print("multi   :", s2, f"chunks={c2}")
    print("embedded:", s3, f"chunks={c3}")
    print("fixtures written to", FIX)


if __name__ == "__main__":
    main()
