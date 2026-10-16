"""A fake OpenAI-compatible image host on a real loopback socket (plan
2026-10-13 Phase 11 step 6): the Phase 10 facade's wire, scripted per request,
so transport drops are real dropped sockets rather than mocked exceptions.

Tests that use it are marked ``local_http`` (conftest allows loopback only)."""

from __future__ import annotations

import base64
import io
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from PIL import Image, PngImagePlugin

WEIGHT_SHA = "a" * 64
RECIPE_SHA = "b" * 64


def facade_provenance(seed: int = 1234, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "recipe": {"id": "hero", "version": 1, "sha256": RECIPE_SHA},
        "models": [
            {
                "id": "z-image-turbo",
                "weights": [{"path": "transformer.safetensors", "sha256": WEIGHT_SHA}],
            }
        ],
        "seed": seed,
        "spdx": "Apache-2.0",
        "prompt": "an empty cafe",
        "reference_digests": [],
        "parent_asset": None,
        "engine_versions": {"diffusers": "0.36.0", "torch": "2.10.0+cu128"},
    }
    record.update(overrides)
    return record


# The facade marks its own PNGs (plan Phase 10). Spelled out here rather than
# imported, so this host stays independent of the code under test.
FACADE_XMP = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
    'xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
    "Iptc4xmpExt:DigitalSourceType="
    '"http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"/>'
    "</rdf:RDF></x:xmpmeta>"
)


def png_b64(marked: bool = True, size: tuple[int, int] = (96, 54)) -> str:
    buf = io.BytesIO()
    kwargs: dict[str, Any] = {}
    if marked:
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", FACADE_XMP, zip=False)
        kwargs["pnginfo"] = info
    Image.new("RGB", size, (30, 90, 160)).save(buf, "PNG", **kwargs)
    return base64.b64encode(buf.getvalue()).decode()


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    json: dict[str, Any] | None
    form: dict[str, str]
    files: list[str]


# A reply writes the whole response itself (status, headers, body, or a drop).
Reply = Callable[[BaseHTTPRequestHandler], None]


@dataclass
class ImageHost:
    replies: list[Reply] = field(default_factory=list)
    received: list[Received] = field(default_factory=list)
    server: ThreadingHTTPServer | None = None

    @property
    def v1_url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def queue(self, *replies: Reply) -> None:
        self.replies.extend(replies)


def _parse(handler: BaseHTTPRequestHandler) -> Received:
    length = int(handler.headers.get("content-length") or 0)
    body = handler.rfile.read(length) if length else b""
    ctype = handler.headers.get("content-type", "")
    parsed_json: dict[str, Any] | None = None
    form: dict[str, str] = {}
    files: list[str] = []
    if ctype.startswith("application/json"):
        parsed_json = json.loads(body)
    elif ctype.startswith("multipart/form-data"):
        msg = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
        )
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files.append(str(name))
            else:
                form[str(name)] = payload.decode()
    return Received(handler.path, dict(handler.headers), parsed_json, form, files)


def start(host: ImageHost) -> ImageHost:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            host.received.append(_parse(self))
            reply = host.replies.pop(0) if host.replies else json_reply(500, {"error": {}})
            reply(self)

        def do_GET(self) -> None:  # noqa: N802
            host.received.append(Received(self.path, dict(self.headers), None, {}, []))
            reply = host.replies.pop(0) if host.replies else json_reply(404, {})
            reply(self)

        def log_message(self, *args: object) -> None:
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request: object, client_address: object) -> None:
            # A scripted drop closes the socket under the handler on purpose.
            pass

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host.server = server
    return host


def stop(host: ImageHost) -> None:
    if host.server is not None:
        host.server.shutdown()
        host.server.server_close()


# ---- replies -----------------------------------------------------------------


def json_reply(status: int, body: object, headers: dict[str, str] | None = None) -> Reply:
    def reply(h: BaseHTTPRequestHandler) -> None:
        data = json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    return reply


RUNNER_GIT_SHA = "200538ff8d9b"


def runner_health(git_sha: str = RUNNER_GIT_SHA) -> Reply:
    """``GET /health`` as a qontinui runner build answers it (the identity
    fields studio checks, inside the runner's envelope)."""
    return json_reply(
        200,
        {
            "success": True,
            "buildId": f"{git_sha}-1791306076603",
            "data": {"status": "ok", "gitSha": git_sha, "buildId": f"{git_sha}-1791306076603"},
        },
    )


def raw_reply(status: int, data: bytes, ctype: str = "application/json") -> Reply:
    def reply(h: BaseHTTPRequestHandler) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    return reply


def sse_event(name: str, payload: object) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def sse_reply(*events: bytes, drop_after: int | None = None) -> Reply:
    """A chunked ``text/event-stream``. ``drop_after=n`` sends n events, then a
    chunk header promising more bytes than follow, and closes the socket: a
    real mid-flight drop (the client sees an incomplete chunked body)."""

    def reply(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        sent = events if drop_after is None else events[:drop_after]
        for e in sent:
            h.wfile.write(_chunk(e))
        if drop_after is not None:
            h.wfile.write(b"400\r\npartial")
            h.wfile.flush()
            h.connection.shutdown(socket.SHUT_RDWR)
            h.close_connection = True
            return
        h.wfile.write(b"0\r\n\r\n")

    return reply


def completed(event: str = "image_generation.completed", **overrides: Any) -> bytes:
    payload: dict[str, Any] = {
        "type": event,
        "b64_json": png_b64(),
        "qontinui_provenance": facade_provenance(),
    }
    payload.update(overrides)
    return sse_event(event, payload)


def progress() -> list[bytes]:
    return [
        sse_event("qontinui.queued", {"type": "qontinui.queued", "position": 1}),
        sse_event("qontinui.running", {"type": "qontinui.running", "step": 1, "total": 9}),
        sse_event("qontinui.running", {"type": "qontinui.running", "step": 9, "total": 9}),
    ]


def refusal(status: int, etype: str, retry_after_s: float | None = None, **extra: Any) -> Reply:
    err: dict[str, Any] = {"type": etype, "code": extra.pop("code", etype), "message": etype}
    if retry_after_s is not None:
        err["retry_after_s"] = retry_after_s
    err.update(extra)
    headers = {"Retry-After": str(int(retry_after_s))} if retry_after_s is not None else {}
    return json_reply(status, {"error": err}, headers)


def closed_port() -> int:
    """A loopback port nothing listens on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def trickle_reply(events: int, interval_s: float) -> Reply:
    """A chunked event stream that keeps sending ``qontinui.running`` progress
    every ``interval_s`` and never completes within ``events`` ticks: a host
    that is alive but never done."""

    def reply(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        try:
            for i in range(events):
                tick = {"type": "qontinui.running", "step": i, "total": events}
                h.wfile.write(_chunk(sse_event("qontinui.running", tick)))
                h.wfile.flush()
                time.sleep(interval_s)
            h.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass  # the client stopped reading: that is the point

    return reply
