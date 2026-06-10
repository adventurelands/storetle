# verified_bridge.py — run verified extraction in another interpreter.
#
# The storetle-verified wheel ships native Lean libraries built for one CPU
# architecture. When the CLI's Python doesn't match (common on Rosetta-era
# Macs: x86_64 Homebrew Python, arm64 Lean libs), extraction can still run
# in a matching interpreter:
#
#   export STORETLE_VERIFIED_PYTHON=/usr/bin/python3
#   export STORETLE_VERIFIED_PYTHONPATH=/path/containing/storetle_verified
#
# The bridge keeps ONE child process alive and streams documents over a
# length-prefixed pipe protocol, so batch extraction doesn't pay a process
# spawn (and Lean runtime load) per document.

import os
import shlex
import struct
import subprocess
import sys

_CHILD_SRC = r'''
import os, struct, sys
p = os.environ.get("STORETLE_VERIFIED_PYTHONPATH")
if p:
    sys.path[:0] = p.split(":")
from storetle_verified import html_to_plaintext
rd, wr = sys.stdin.buffer, sys.stdout.buffer
while True:
    hdr = rd.read(4)
    if len(hdr) < 4:
        break
    n = struct.unpack(">I", hdr)[0]
    if n == 0:
        break
    doc = rd.read(n)
    try:
        out = html_to_plaintext(doc).encode("utf-8")
        wr.write(b"\x00" + struct.pack(">I", len(out)) + out)
    except Exception as e:
        msg = str(e).encode("utf-8", "replace")
        wr.write(b"\x01" + struct.pack(">I", len(msg)) + msg)
    wr.flush()
'''


class VerifiedBridge:
    """Verified extraction via a persistent subprocess interpreter."""

    def __init__(self, python=None):
        self._python = python or os.environ.get('STORETLE_VERIFIED_PYTHON')
        self._proc = None

    @property
    def configured(self):
        return bool(self._python)

    def _ensure(self):
        if self._proc is None or self._proc.poll() is not None:
            # the env var may be a command line, e.g. "arch -arm64 /usr/bin/python3"
            # (on macOS a universal child otherwise inherits a Rosetta
            # parent's x86_64 slice)
            cmd = shlex.split(self._python) + ['-c', _CHILD_SRC]
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=sys.stderr.fileno())
        return self._proc

    def extract(self, html_bytes):
        p = self._ensure()
        p.stdin.write(struct.pack('>I', len(html_bytes)) + html_bytes)
        p.stdin.flush()
        status = p.stdout.read(1)
        if not status:
            raise RuntimeError(
                'verified bridge interpreter exited unexpectedly '
                f'({self._python})')
        n = struct.unpack('>I', p.stdout.read(4))[0]
        payload = p.stdout.read(n)
        if status == b'\x01':
            raise ValueError(payload.decode('utf-8', 'replace'))
        return payload

    def close(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write(struct.pack('>I', 0))
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None
