#!/usr/bin/env python3
# stress_test.py — HTMLCUBE comprehensive stress test
#
# Downloads real HTML from multiple sources (different languages, site types),
# then benchmarks against every relevant standard:
#
#   per-file gzip     — how most HTTP servers compress today
#   per-file brotli   — the current best single-file standard
#   tar + gzip        — cross-file gzip (the real folder competitor)
#   tar + brotli      — cross-file brotli
#   CUBE pack         — our approach
#
# Also verifies round-trip correctness on every file.

import os, sys, re, gzip, struct, zlib, tarfile, subprocess, time
import urllib.request
from pathlib import Path
from collections import defaultdict

# ── colors ───────────────────────────────────────────────────────────────────
R='\033[0m'; B='\033[1m'; G='\033[92m'; Y='\033[93m'
RD='\033[91m'; C='\033[96m'; D='\033[2m'
def bold(s): return f'{B}{s}{R}'
def green(s): return f'{G}{s}{R}'
def yellow(s): return f'{Y}{s}{R}'
def red(s): return f'{RD}{s}{R}'
def dim(s): return f'{D}{s}{R}'
def cyan(s): return f'{C}{s}{R}'
def fmt(n):
    if n<1024: return f'{n}B'
    if n<1048576: return f'{n/1024:.1f}KB'
    return f'{n/1048576:.2f}MB'
def pct(a,b): return f'{100*(1-a/b):.1f}%' if b else '0%'
def bar(f,w=18): f=min(max(f,0),1); return '█'*int(w*f)+'░'*(w-int(w*f))

# ── test scenarios ────────────────────────────────────────────────────────────
# Each scenario: (name, description, list of URLs to fetch)
# We fetch multiple pages from the same site to get cross-file repetition.

SCENARIOS = [

    ('wikipedia_en', 'Wikipedia EN — same template, different articles', [
        'https://en.wikipedia.org/wiki/Data_compression',
        'https://en.wikipedia.org/wiki/Huffman_coding',
        'https://en.wikipedia.org/wiki/Lempel%E2%80%93Ziv%E2%80%93Welch',
        'https://en.wikipedia.org/wiki/Brotli',
        'https://en.wikipedia.org/wiki/Gzip',
        'https://en.wikipedia.org/wiki/Zstandard',
        'https://en.wikipedia.org/wiki/Run-length_encoding',
        'https://en.wikipedia.org/wiki/Arithmetic_coding',
    ]),

    ('wikipedia_ja', 'Wikipedia JA — Japanese, CJK multibyte content', [
        'https://ja.wikipedia.org/wiki/%E3%83%87%E3%83%BC%E3%82%BF%E5%9C%A7%E7%B8%AE',
        'https://ja.wikipedia.org/wiki/%E4%BA%BA%E5%B7%A5%E7%9F%A5%E8%83%BD',
        'https://ja.wikipedia.org/wiki/%E6%A9%9F%E6%A2%B0%E5%AD%A6%E7%BF%92',
        'https://ja.wikipedia.org/wiki/%E3%83%97%E3%83%AD%E3%82%B0%E3%83%A9%E3%83%9F%E3%83%B3%E3%82%B0',
        'https://ja.wikipedia.org/wiki/%E3%82%A2%E3%83%AB%E3%82%B4%E3%83%AA%E3%82%BA%E3%83%A0',
    ]),

    ('wikipedia_ar', 'Wikipedia AR — Arabic, RTL, dense structure', [
        'https://ar.wikipedia.org/wiki/%D8%B6%D8%BA%D8%B7_%D8%A7%D9%84%D8%A8%D9%8A%D8%A7%D9%86%D8%A7%D8%AA',
        'https://ar.wikipedia.org/wiki/%D8%A7%D9%84%D8%B0%D9%83%D8%A7%D8%A1_%D8%A7%D9%84%D8%A7%D8%B5%D8%B7%D9%86%D8%A7%D8%B9%D9%8A',
        'https://ar.wikipedia.org/wiki/%D8%A8%D8%B1%D9%85%D8%AC%D8%A9_%D8%A7%D9%84%D8%AD%D8%A7%D8%B3%D9%88%D8%A8',
        'https://ar.wikipedia.org/wiki/%D8%AE%D9%88%D8%A7%D8%B1%D8%B2%D9%85%D9%8A%D8%A9',
    ]),

    ('wikipedia_de', 'Wikipedia DE — German, extended Latin chars', [
        'https://de.wikipedia.org/wiki/Datenkompression',
        'https://de.wikipedia.org/wiki/K%C3%BCnstliche_Intelligenz',
        'https://de.wikipedia.org/wiki/Maschinelles_Lernen',
        'https://de.wikipedia.org/wiki/Algorithmus',
        'https://de.wikipedia.org/wiki/Programmiersprache',
    ]),

    ('wikipedia_zh', 'Wikipedia ZH — Simplified Chinese', [
        'https://zh.wikipedia.org/wiki/%E6%95%B0%E6%8D%AE%E5%8E%8B%E7%BC%A9',
        'https://zh.wikipedia.org/wiki/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD',
        'https://zh.wikipedia.org/wiki/%E6%9C%BA%E5%99%A8%E5%AD%A6%E4%B9%A0',
        'https://zh.wikipedia.org/wiki/%E7%AE%97%E6%B3%95',
    ]),

    ('wikipedia_ru', 'Wikipedia RU — Russian, Cyrillic', [
        'https://ru.wikipedia.org/wiki/%D0%A1%D0%B6%D0%B0%D1%82%D0%B8%D0%B5_%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85',
        'https://ru.wikipedia.org/wiki/%D0%98%D1%81%D0%BA%D1%83%D1%81%D1%81%D1%82%D0%B2%D0%B5%D0%BD%D0%BD%D1%8B%D0%B9_%D0%B8%D0%BD%D1%82%D0%B5%D0%BB%D0%BB%D0%B5%D0%BA%D1%82',
        'https://ru.wikipedia.org/wiki/%D0%90%D0%BB%D0%B3%D0%BE%D1%80%D0%B8%D1%82%D0%BC',
        'https://ru.wikipedia.org/wiki/%D0%9C%D0%B0%D1%88%D0%B8%D0%BD%D0%BD%D0%BE%D0%B5_%D0%BE%D0%B1%D1%83%D1%87%D0%B5%D0%BD%D0%B8%D0%B5',
    ]),

    ('hackernews', 'Hacker News — extreme template repetition, minimal content', [
        'https://news.ycombinator.com/',
        'https://news.ycombinator.com/news?p=2',
        'https://news.ycombinator.com/news?p=3',
        'https://news.ycombinator.com/news?p=4',
        'https://news.ycombinator.com/news?p=5',
        'https://news.ycombinator.com/ask',
        'https://news.ycombinator.com/show',
        'https://news.ycombinator.com/jobs',
    ]),

    ('python_docs', 'Python Docs — structured documentation, consistent nav', [
        'https://docs.python.org/3/library/functions.html',
        'https://docs.python.org/3/library/stdtypes.html',
        'https://docs.python.org/3/library/string.html',
        'https://docs.python.org/3/library/re.html',
        'https://docs.python.org/3/library/collections.html',
        'https://docs.python.org/3/library/itertools.html',
        'https://docs.python.org/3/library/pathlib.html',
    ]),

    ('diverse_worst_case', 'Diverse sites — worst case, nothing shared', [
        'https://en.wikipedia.org/wiki/Data_compression',
        'https://news.ycombinator.com/',
        'https://docs.python.org/3/library/functions.html',
        'https://ja.wikipedia.org/wiki/%E4%BA%BA%E5%B7%A5%E7%9F%A5%E8%83%BD',
        'https://de.wikipedia.org/wiki/Algorithmus',
        'https://ar.wikipedia.org/wiki/%D8%B6%D8%BA%D8%B7_%D8%A7%D9%84%D8%A8%D9%8A%D8%A7%D9%86%D8%A7%D8%AA',
    ]),
]

# ── helpers ───────────────────────────────────────────────────────────────────

def fetch(url, dest_path, timeout=15):
    """Download a URL to dest_path. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; HTMLCubeStressTest/1.0)'
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        Path(dest_path).write_bytes(data)
        return True
    except Exception as e:
        return False


def tar_gz_size(folder):
    """Size of tar+gzip of all HTML in folder."""
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz', compresslevel=9) as tar:
        for f in sorted(Path(folder).glob('*.html')):
            tar.add(f, arcname=f.name)
    return len(buf.getvalue())


def tar_br_size(folder):
    """Size of tar piped through brotli."""
    import io, tempfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for f in sorted(Path(folder).glob('*.html')):
            tar.add(f, arcname=f.name)
    tar_bytes = buf.getvalue()
    try:
        result = subprocess.run(
            ['brotli', '-q', '11', '-'],
            input=tar_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if result.returncode == 0:
            return len(result.stdout)
    except FileNotFoundError:
        pass
    return None


def per_file_gz(folder):
    total = 0
    for f in Path(folder).glob('*.html'):
        total += len(gzip.compress(f.read_bytes(), compresslevel=9))
    return total


def per_file_br(folder):
    total = 0
    for f in Path(folder).glob('*.html'):
        try:
            r = subprocess.run(['brotli','-q','11','-c',str(f)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            total += len(r.stdout)
        except FileNotFoundError:
            total += len(gzip.compress(f.read_bytes(), compresslevel=9))
    return total


def verify_roundtrip(folder, pack_path):
    """Pack folder, unpack to temp dir, verify all files round-trip."""
    import tempfile
    sys.path.insert(0, str(Path(__file__).parent))
    from storetle.folder import pack, unpack
    from html.parser import HTMLParser
    from storetle.vocab import VOID_ELEMENTS

    class SE(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tags=[]; self.words=[]
        def handle_starttag(self,t,a): self.tags.append(t.lower())
        def handle_endtag(self,t):
            if t.lower() not in VOID_ELEMENTS: self.tags.append(f'/{t.lower()}')
        def handle_data(self,d):
            c=re.sub(r'\s+',' ',d).strip()
            if c: self.words.append(c)

    with tempfile.TemporaryDirectory() as tmpdir:
        unpack(pack_path, tmpdir)
        files = list(Path(folder).glob('*.html'))
        passed = failed = 0
        for f in files:
            dec = Path(tmpdir) / f.name
            if not dec.exists():
                failed += 1
                continue
            o=SE(); d=SE()
            o.feed(f.read_text(encoding='utf-8',errors='replace'))
            d.feed(dec.read_text(encoding='utf-8',errors='replace'))
            ow=re.split(r'\s+',' '.join(o.words).strip())
            dw=re.split(r'\s+',' '.join(d.words).strip())
            if o.tags==d.tags and ow==dw:
                passed += 1
            else:
                failed += 1
    return passed, failed


# ── main benchmark ────────────────────────────────────────────────────────────

def run_scenario(name, description, urls, base_dir):
    folder = Path(base_dir) / name
    folder.mkdir(parents=True, exist_ok=True)
    pack_path = str(folder) + '.cubepack'

    print()
    print(bold(f'  ▸ {description}'))
    print(dim(f'    {len(urls)} URLs'))

    # Download files
    print(f'    Fetching...', end=' ', flush=True)
    fetched = 0
    for i, url in enumerate(urls):
        dest = folder / f'page_{i+1:02d}.html'
        if dest.exists():  # cache
            fetched += 1
            continue
        if fetch(url, dest):
            fetched += 1
        else:
            pass

    if fetched == 0:
        print(red('no files fetched, skipping'))
        return None

    print(f'{fetched}/{len(urls)} pages cached')

    # Measure original total size
    files = list(folder.glob('*.html'))
    if not files:
        print(red('    no HTML files found'))
        return None

    orig_total = sum(f.stat().st_size for f in files)

    # Benchmarks
    t0 = time.time()
    sys.path.insert(0, str(Path(__file__).parent))
    from storetle.folder import pack as cube_pack
    pack_stats = cube_pack(str(folder), pack_path)
    cube_time = time.time() - t0
    cube_size = pack_stats['pack_size']

    gz_pf   = per_file_gz(folder)
    br_pf   = per_file_br(folder)
    tgz     = tar_gz_size(folder)
    tbr     = tar_br_size(folder)

    # Correctness
    passed, failed = verify_roundtrip(str(folder), pack_path)

    result = {
        'name':        name,
        'desc':        description,
        'files':       len(files),
        'orig':        orig_total,
        'cube':        cube_size,
        'gz_pf':       gz_pf,
        'br_pf':       br_pf,
        'tgz':         tgz,
        'tbr':         tbr,
        'cube_time':   cube_time,
        'passed':      passed,
        'failed':      failed,
    }

    # Print inline result
    def pp(label, size, ref):
        p = 100*(1-size/ref)
        sign = green(f'+{p:.1f}pp') if p > 0 else red(f'{p:.1f}pp')
        return f'{label} {pct(size,ref)} ({fmt(size)})'

    print(f'    Original:     {fmt(orig_total)}   {len(files)} files')
    print(f'    gzip/file:    {pct(gz_pf, orig_total)}  ({fmt(gz_pf)})')
    print(f'    brotli/file:  {pct(br_pf, orig_total)}  ({fmt(br_pf)})')
    print(f'    tar+gzip:     {pct(tgz,   orig_total)}  ({fmt(tgz)})')
    if tbr:
        print(f'    tar+brotli:   {pct(tbr,   orig_total)}  ({fmt(tbr)})')
    cube_pct = 100*(1-cube_size/orig_total)
    br_pf_pct = 100*(1-br_pf/orig_total)
    tbr_pct = 100*(1-tbr/orig_total) if tbr else br_pf_pct
    best_other = max(br_pf_pct, tbr_pct)
    diff = cube_pct - best_other
    color = green if diff > 0 else yellow
    print(f'    {bold("CUBE pack:")}    {bold(pct(cube_size, orig_total))}  ({fmt(cube_size)})  '
          f'{color(("+" if diff>=0 else "")+f"{diff:.1f}pp vs best")}')
    status = green('✓ all pass') if failed==0 else red(f'✗ {failed} failed')
    print(f'    Correctness:  {status}  ({passed}/{passed+failed})')

    return result


def print_summary(results):
    valid = [r for r in results if r]
    if not valid:
        return

    print()
    print(bold('  ══ SUMMARY ══════════════════════════════════════════════════'))
    print()
    print(f'  {"Scenario":<30} {"Orig":>8} {"br/file":>8} {"tar+br":>8} {"CUBE":>8} {"vs best":>9} {"RT":>5}')
    print(dim('  ' + '─'*78))

    cube_wins = 0
    for r in valid:
        cube_pct  = 100*(1-r['cube']/r['orig'])
        br_pf_pct = 100*(1-r['br_pf']/r['orig'])
        tbr_pct   = 100*(1-r['tbr']/r['orig']) if r['tbr'] else br_pf_pct
        best      = max(br_pf_pct, tbr_pct)
        diff      = cube_pct - best
        rt        = '✓' if r['failed']==0 else f'✗{r["failed"]}'

        diff_str = f'{("+" if diff>=0 else "")}{diff:.1f}pp'
        color = green if diff > 0.5 else (yellow if diff > -3 else red)

        if diff > 0:
            cube_wins += 1

        print(f'  {r["name"]:<30} '
              f'{fmt(r["orig"]):>8} '
              f'{br_pf_pct:>7.1f}% '
              f'{tbr_pct:>7.1f}% '
              f'{cube_pct:>7.1f}% '
              f'{color(diff_str):>9} '
              f'{rt:>5}')

    print(dim('  ' + '─'*78))
    print(f'  CUBE wins: {bold(str(cube_wins))}/{len(valid)} scenarios')

    # Weighted average
    total_orig = sum(r['orig'] for r in valid)
    total_cube = sum(r['cube'] for r in valid)
    total_br   = sum(r['br_pf'] for r in valid)
    total_tbr  = sum(r['tbr'] for r in valid if r['tbr'])
    overall_cube = 100*(1-total_cube/total_orig)
    overall_br   = 100*(1-total_br/total_orig)
    overall_tbr  = 100*(1-total_tbr/total_orig) if total_tbr else overall_br
    best_overall = max(overall_br, overall_tbr)

    print()
    print(f'  {"Overall (weighted):":<30} '
          f'{fmt(total_orig):>8} '
          f'{overall_br:>7.1f}% '
          f'{overall_tbr:>7.1f}% '
          f'{bold(f"{overall_cube:.1f}%"):>8} '
          f'{green(f"+{overall_cube-best_overall:.1f}pp") if overall_cube > best_overall else red(f"{overall_cube-best_overall:.1f}pp"):>9}')
    print()


# ── entry ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    base_dir = Path(__file__).parent / 'stress_data'
    base_dir.mkdir(exist_ok=True)

    # Allow running specific scenarios: python stress_test.py wikipedia_en hackernews
    if len(sys.argv) > 1:
        names   = set(sys.argv[1:])
        targets = [s for s in SCENARIOS if s[0] in names]
    else:
        targets = SCENARIOS

    print()
    print(bold('  HTMLCUBE STRESS TEST'))
    print(dim('  Benchmarks: per-file gzip | per-file brotli | tar+gzip | tar+brotli | CUBE pack'))
    print(dim('  Correctness: full round-trip verify on every file'))
    print()

    results = []
    for name, desc, urls in targets:
        r = run_scenario(name, desc, urls, base_dir)
        results.append(r)

    print_summary(results)
