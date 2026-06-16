"""Persistent `lake env lean --server` daemon per Lake project root.

Keeps Mathlib oleans mmapped in one long-running Lean LSP server process
instead of cold-spawning `lake env lean <file>` for every `lean_check` call.

Headline win measured on FQB: ~0.21 s per in-place edit vs. ~88 s for cold
subprocess. See `tests/lsp/README.md` for the benchmark.

Falls back transparently to the caller (which should re-run via subprocess)
on any LSP error or server crash. Set `LEA_DISABLE_LSP=1` to skip entirely.
"""
from __future__ import annotations
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Queue, Empty


# Restart a daemon after this many checks. Bounds memory growth from Lean's
# per-document elaboration cache. Tunable via env var.
_RESTART_AFTER = int(os.environ.get("LEA_LSP_RESTART_AFTER", "500"))

# Hard timeout for one check (incl. cold first-open). Subsumed by the
# caller's LEAN_CHECK_TIMEOUT if larger.
_CHECK_TIMEOUT = int(os.environ.get("LEAN_CHECK_TIMEOUT", "900"))

# Mapping from LSP severity int → string matching `lake env lean` output.
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _encode(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class LeanDaemon:
    """One `lake env lean --server` instance scoped to a Lake project root."""

    def __init__(self, lake_root: str):
        self.lake_root = lake_root
        self.proc: subprocess.Popen | None = None
        self.queue: Queue = Queue()
        self.opened: set[str] = set()  # file URIs seen via didOpen
        self.next_id = 0
        self.calls = 0
        self.broken = False

    def start(self) -> bool:
        """Spawn server and run LSP handshake. Returns False on failure."""
        try:
            self.proc = subprocess.Popen(
                ["lake", "env", "lean", "--server"],
                cwd=self.lake_root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,  # own process group so we can kill workers too
            )
        except FileNotFoundError:
            return False

        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        try:
            self._send({
                "jsonrpc": "2.0", "id": self._nxt(), "method": "initialize",
                "params": {
                    "processId": os.getpid(),
                    "rootUri": Path(self.lake_root).as_uri(),
                    "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                },
            })
            # wait for initialize response (id=1)
            t0 = time.time()
            while time.time() - t0 < 120:
                try:
                    m = self.queue.get(timeout=2)
                except Empty:
                    continue
                if m is None:
                    return False
                if m.get("id") == 1:
                    break
            else:
                return False
            self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            return True
        except (BrokenPipeError, OSError):
            return False

    def check(self, file_path: str, content: str, progress_cb=None) -> str:
        """Open or update a file with `content`, wait for diagnostics, return them.

        progress_cb: optional callable(str) called with a human-readable message
        each time Lean reports a new elaboration frontier via $/lean/fileProgress.
        """
        if self.broken or self.proc is None:
            raise RuntimeError("daemon not alive")

        uri = Path(file_path).as_uri()
        version = self.calls + 1
        self.calls += 1

        # purge stale notifications from prior calls
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                break

        try:
            if uri in self.opened:
                self._send({
                    "jsonrpc": "2.0", "method": "textDocument/didChange",
                    "params": {
                        "textDocument": {"uri": uri, "version": version},
                        "contentChanges": [{"text": content}],
                    },
                })
            else:
                self._send({
                    "jsonrpc": "2.0", "method": "textDocument/didOpen",
                    "params": {"textDocument": {
                        "uri": uri, "languageId": "lean4",
                        "version": version, "text": content,
                    }},
                })
                self.opened.add(uri)
        except (BrokenPipeError, OSError):
            self.broken = True
            raise

        # Wait for elaboration to finish. Two signals we combine:
        #   - publishDiagnostics: carries the result; Lean may send preliminary
        #     empty ones during cold loads, then the real one at the end.
        #   - $/lean/fileProgress with empty processing: "elaboration done".
        #     For warm didChange this often arrives BEFORE publishDiagnostics.
        # Return as soon as we have a definitive answer (see should_return).
        t0 = time.time()
        last_diags = None
        saw_progress_empty = False
        last_heartbeat = t0
        while time.time() - t0 < _CHECK_TIMEOUT:
            try:
                m = self.queue.get(timeout=2)
            except Empty:
                if self.proc.poll() is not None:
                    self.broken = True
                    raise RuntimeError(f"server exited (code {self.proc.returncode})")
                now = time.time()
                if progress_cb and now - last_heartbeat >= 5:
                    last_heartbeat = now
                    progress_cb(f"waiting… ({now - t0:.0f}s)")
                continue
            if m is None:
                self.broken = True
                raise RuntimeError("server stream closed")
            method = m.get("method")
            params = m.get("params", {})
            if method == "textDocument/publishDiagnostics" and params.get("uri") == uri:
                last_diags = params.get("diagnostics", [])
                # Definitive when: fileProgress=empty already arrived, OR
                # diagnostics are non-empty (Lean only emits non-empty when
                # it's the real result — preliminary "clearing" frames are
                # always []).
                if saw_progress_empty or len(last_diags) > 0:
                    return self._format(file_path, self._drain_followups(uri, last_diags))
            elif method == "$/lean/fileProgress" and params.get("textDocument", {}).get("uri") == uri:
                processing = params.get("processing", [])
                if not processing:
                    saw_progress_empty = True
                    if last_diags is not None:
                        return self._format(file_path, self._drain_followups(uri, last_diags))
                elif progress_cb:
                    frontier = min(
                        r.get("range", {}).get("start", {}).get("line", 0)
                        for r in processing
                    ) + 1  # 1-indexed
                    progress_cb(f"elaborating line {frontier}…")
        self.broken = True
        raise RuntimeError(f"daemon timeout after {_CHECK_TIMEOUT}s")

    def _drain_followups(self, uri: str, current: list) -> list:
        """After a return-trigger, briefly listen for trailing publishDiagnostics
        updates so we don't return a stale partial result."""
        quiet_until = time.time() + 0.3
        while time.time() < quiet_until:
            try:
                m = self.queue.get(timeout=max(0.01, quiet_until - time.time()))
            except Empty:
                break
            if m is None:
                break
            if (m.get("method") == "textDocument/publishDiagnostics"
                    and m.get("params", {}).get("uri") == uri):
                current = m["params"].get("diagnostics", [])
                quiet_until = time.time() + 0.3
        return current

    def shutdown(self):
        if self.proc is None:
            return
        try:
            self._send({"jsonrpc": "2.0", "id": self._nxt(), "method": "shutdown"})
            self._send({"jsonrpc": "2.0", "method": "exit"})
            self.proc.wait(timeout=5)
        except Exception:
            pass
        finally:
            # Kill the entire process group to clean up lean --worker children.
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.broken = True

    # ---- internal ----
    def _nxt(self) -> int:
        self.next_id += 1
        return self.next_id

    def _send(self, msg: dict):
        self.proc.stdin.write(_encode(msg))
        self.proc.stdin.flush()

    def _reader(self):
        try:
            stream = self.proc.stdout
            while True:
                headers = {}
                while True:
                    line = stream.readline()
                    if not line:
                        self.queue.put(None)
                        return
                    s = line.decode("utf-8", errors="replace").strip()
                    if s == "":
                        break
                    k, _, v = s.partition(":")
                    headers[k.strip().lower()] = v.strip()
                n = int(headers.get("content-length", 0))
                if n == 0:
                    continue
                body = stream.read(n).decode("utf-8", errors="replace")
                try:
                    self.queue.put(json.loads(body))
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            print(f"  [lean-lsp] _reader crashed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            self.queue.put(None)

    def _drain_stderr(self):
        for _ in iter(self.proc.stderr.readline, b""):
            pass  # discard; LSP server stderr is mostly info noise

    def _format(self, file_path: str, diags: list) -> str:
        # Only surface errors (severity 1) and warnings (severity 2) to match
        # subprocess `lake env lean` behavior. LSP info/hint diagnostics (3/4)
        # are hover-style metadata that confuse the agent (it interpreted an
        # `info:` path-resolution note as a real problem and went off-rails).
        diags = [d for d in diags if d.get("severity", 1) in (1, 2)]
        if not diags:
            return "OK — no errors, no warnings."
        lines = []
        for d in diags:
            sev = _SEVERITY.get(d.get("severity", 1), "error")
            r = d.get("range", {}).get("start", {})
            ln = r.get("line", 0) + 1
            col = r.get("character", 0) + 1
            msg = d.get("message", "").rstrip()
            lines.append(f"{file_path}:{ln}:{col}: {sev}: {msg}")
        return "\n".join(lines)


# ---- module-level cache ----
_daemons: dict[str, LeanDaemon] = {}
_lock = threading.Lock()


def check_via_lsp(file_path: str, content: str, lake_root: str, progress_cb=None) -> str:
    """Run `lean_check` via the persistent LSP daemon for `lake_root`.

    Raises on any failure so the caller can fall back to subprocess.
    progress_cb: optional callable(str) forwarded to LeanDaemon.check().
    """
    with _lock:
        d = _daemons.get(lake_root)
        if d and (d.broken or d.calls >= _RESTART_AFTER):
            d.shutdown()
            del _daemons[lake_root]
            d = None
        if d is None:
            if progress_cb:
                progress_cb("starting LSP daemon…")
            d = LeanDaemon(lake_root)
            if not d.start():
                raise RuntimeError(f"failed to start lean --server in {lake_root}")
            _daemons[lake_root] = d
    return d.check(file_path, content, progress_cb=progress_cb)


@atexit.register
def _shutdown_all():
    for d in list(_daemons.values()):
        d.shutdown()
    _daemons.clear()
