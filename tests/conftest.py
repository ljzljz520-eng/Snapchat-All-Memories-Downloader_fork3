"""Shared test fixtures: minimal JPEG/mp4 payloads, fake CDN, subprocess runs."""

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Minimal valid 1x1 baseline JPEG (no EXIF segment). Verified re-parseable by
# the exif library after tag injection.
JPEG_BYTES = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c23"
    "1c1c2837292c30313434341f27393d38323c2e333432ffc0000b08000100010101"
    "1100ffc4001f000001050101010101010000000000000000010203040506070809"
    "0a0bffc400b5100002010303020403050504040000017d01020300041105122131"
    "410613516107227114328191a1082342b1c11552d1f02433627282090a16171819"
    "1a25262728292a3435363738393a434445464748494a535455565758595a636465"
    "666768696a737475767778797a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9da"
    "e1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbffd9"
)
MP4_BYTES = b"\x00\x00\x00\x1cftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _no_ambient_fault(monkeypatch):
    """In-process tests must never inherit an armed fault point."""
    monkeypatch.delenv("MEMORIES_FAULT", raising=False)


def make_raw_record(
    date: str = "2020-01-02 03:04:05 UTC",
    name: str = "pixel.jpg",
    location: str = "",
):
    return {
        "Date": date,
        "Download Link": f"https://link.test/obj/{name}",
        "Location": location,
    }


def write_json(path: Path, records: list[dict]) -> Path:
    path.write_text(
        json.dumps({"Saved Media": records}), encoding="utf-8"
    )
    return path


class FakeCDN:
    """httpx mock: POST resolves the object name; GET serves fixture bytes."""

    def __init__(self, payloads: dict[str, bytes] | None = None):
        self.payloads = payloads or {
            "/pixel.jpg": JPEG_BYTES,
            "/clip.mp4": MP4_BYTES,
        }
        self.posts = 0
        self.gets = 0
        self.fail_paths: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        cdn = self

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                cdn.posts += 1
                name = request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(
                    200, text=f"https://cdn.test/{name}?sig=token"
                )
            cdn.gets += 1
            path = request.url.path
            if path in cdn.fail_paths:
                return httpx.Response(500, text="injected server error")
            payload = cdn.payloads.get(path)
            if payload is None:
                return httpx.Response(404, text="missing fixture")
            return httpx.Response(200, content=payload)

        return httpx.MockTransport(handler)


@pytest.fixture
def cdn():
    return FakeCDN()


# ---------------------------------------------------------------------------
# Real loopback HTTP server for cross-process (subprocess) crash tests.
# ---------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.posts += 1
        name = self.path.rsplit("/", 1)[-1]
        body = f"http://127.0.0.1:{self.server.server_port}/{name}?sig=1".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.gets += 1
        path = urllib.parse.urlparse(self.path).path
        payload = self.server.payloads.get(path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, addr, payloads):
        super().__init__(addr, _Handler)
        self.payloads = payloads
        self.posts = 0
        self.gets = 0


@pytest.fixture
def loopback_server():
    server = _Server(
        ("127.0.0.1", 0), {"/pixel.jpg": JPEG_BYTES, "/clip.mp4": MP4_BYTES}
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def loopback_record(server: _Server, name: str = "pixel.jpg", location: str = ""):
    return {
        "Date": "2020-01-02 03:04:05 UTC",
        "Download Link": f"http://127.0.0.1:{server.server_port}/obj/{name}",
        "Location": location,
    }


def run_cli(json_path: Path, out_dir: Path, *extra_args: str, fault: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "MEMORIES_FAULT"}
    if fault:
        env["MEMORIES_FAULT"] = fault
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "main.py"),
            str(json_path),
            "-o",
            str(out_dir),
            "-c",
            "2",
            *extra_args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
