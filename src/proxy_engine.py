"""Loopback HTTP/HTTPS proxy that assigns each new TCP connection to a NIC."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from select import select
import socket
import socketserver
import struct
import threading
from urllib.parse import urlsplit


HTTP_PROXY_HOST = "127.0.0.1"
HTTP_PROXY_PORT = 10801
SOCKS_PROXY_PORT = 10800
MAX_CLIENTS = 64
MAX_HEADER_BYTES = 64 * 1024
IP_UNICAST_IF = getattr(socket, "IP_UNICAST_IF", 31)


@dataclass(frozen=True)
class AdapterPath:
    name: str
    interface_index: int
    ipv4_address: str
    gateway: str


class PathScheduler:
    """Small, lock-protected round-robin scheduler for new proxy sessions."""

    def __init__(self, paths: list[AdapterPath]):
        if len(paths) < 2:
            raise ValueError("Connect Ethernet and Wi-Fi before starting Boost.")
        self._paths = tuple(paths)
        self._index = 0
        self._lock = threading.Lock()

    def ordered_paths(self) -> tuple[AdapterPath, ...]:
        with self._lock:
            start = self._index
            self._index = (self._index + 1) % len(self._paths)
        return self._paths[start:] + self._paths[:start]


def _open_on_path(path: AdapterPath, host: str, port: int) -> socket.socket:
    """Open an IPv4 connection pinned to an adapter's interface and address."""
    errors = []
    for family, socktype, proto, _, address in socket.getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(10)
            # Windows requires the interface index in network byte order.
            sock.setsockopt(
                socket.IPPROTO_IP,
                IP_UNICAST_IF,
                socket.htonl(path.interface_index),
            )
            sock.bind((path.ipv4_address, 0))
            sock.connect(address)
            sock.settimeout(1)
            return sock
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            sock.close()
    raise OSError("; ".join(errors) or "No IPv4 address was returned for the host.")


def _connect_any(scheduler: PathScheduler, host: str, port: int) -> tuple[socket.socket, AdapterPath]:
    errors = []
    for path in scheduler.ordered_paths():
        try:
            return _open_on_path(path, host, port), path
        except OSError as exc:
            errors.append(str(exc))
    raise OSError("No selected adapter could connect: " + " | ".join(errors))


def _read_headers(sock: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    sock.settimeout(15)
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Client closed before sending request headers.")
        data.extend(chunk)
        if len(data) > MAX_HEADER_BYTES:
            raise ValueError("Proxy request headers are too large.")
    sock.settimeout(1)
    end = data.index(b"\r\n\r\n") + 4
    return bytes(data[:end]), bytes(data[end:])


def _send_http_error(sock: socket.socket, status: HTTPStatus, detail: str) -> None:
    body = (detail + "\n").encode("utf-8", errors="replace")
    response = (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    try:
        sock.sendall(response)
    except OSError:
        pass


def _parse_authority(authority: str, default_port: int) -> tuple[str, int]:
    authority = authority.strip()
    if not authority or authority.startswith("["):
        raise ValueError("IPv6 literal destinations are not supported by this first proxy build.")
    host, separator, port_text = authority.rpartition(":")
    if not separator:
        host, port = authority, default_port
    else:
        if not port_text.isdecimal():
            raise ValueError("Invalid destination port.")
        port = int(port_text)
    if not host or not 1 <= port <= 65535:
        raise ValueError("Invalid destination host or port.")
    return host, port


def _relay(client: socket.socket, upstream: socket.socket, stop_event: threading.Event,
           initial_to_upstream: bytes = b"") -> None:
    if initial_to_upstream:
        upstream.sendall(initial_to_upstream)
    while not stop_event.is_set():
        readable, _, exceptional = select([client, upstream], [], [client, upstream], 0.5)
        if exceptional:
            return
        for source in readable:
            destination = upstream if source is client else client
            try:
                chunk = source.recv(64 * 1024)
            except (ConnectionError, OSError):
                return
            if not chunk:
                return
            try:
                destination.sendall(chunk)
            except OSError:
                return


class _BoundedThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, owner):
        self.owner = owner
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(address, handler, bind_and_activate=True)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _HTTPProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        upstream = None
        try:
            header, initial_body = _read_headers(client)
            lines = header[:-4].split(b"\r\n")
            parts = lines[0].decode("latin-1").split(" ", 2)
            if len(parts) != 3:
                _send_http_error(client, HTTPStatus.BAD_REQUEST, "Malformed HTTP request line.")
                return
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, port = _parse_authority(target, 443)
                upstream, path = _connect_any(self.server.owner.scheduler, host, port)
                client.sendall(
                    f"{version} 200 Connection established\r\n"
                    f"X-NetFusion-Adapter: {path.name}\r\n\r\n".encode("latin-1")
                )
                _relay(client, upstream, self.server.owner.stop_event, initial_body)
                return

            parsed = urlsplit(target)
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                _send_http_error(
                    client, HTTPStatus.BAD_REQUEST,
                    "Use an HTTP proxy request or HTTPS CONNECT; IPv6 literals are not supported.",
                )
                return
            host = parsed.hostname
            if ":" in host:
                raise ValueError("IPv6 literal destinations are not supported by this first proxy build.")
            port = parsed.port or 80
            if not 1 <= port <= 65535:
                raise ValueError("Invalid destination port.")
            upstream, _ = _connect_any(self.server.owner.scheduler, host, port)
            path_and_query = parsed.path or "/"
            if parsed.query:
                path_and_query += "?" + parsed.query
            output = [f"{method} {path_and_query} {version}\r\n"]
            for raw_line in lines[1:]:
                name = raw_line.split(b":", 1)[0].strip().lower()
                if name not in (b"proxy-authorization", b"proxy-connection", b"connection"):
                    output.append(raw_line.decode("latin-1") + "\r\n")
            output.append("Connection: close\r\n\r\n")
            upstream.sendall("".join(output).encode("latin-1") + initial_body)
            _relay(client, upstream, self.server.owner.stop_event)
        except (OSError, ValueError, UnicodeError, ConnectionError) as exc:
            _send_http_error(client, HTTPStatus.BAD_GATEWAY, f"NetFusion could not open this connection: {exc}")
        finally:
            if upstream is not None:
                upstream.close()


class _SOCKS5Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        upstream = None
        try:
            client.settimeout(10)
            greeting = self._recv_exact(client, 2)
            if len(greeting) != 2 or greeting[0] != 5:
                return
            methods = self._recv_exact(client, greeting[1])
            if 0 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x00")
            header = self._recv_exact(client, 4)
            if len(header) != 4 or header[:3] != b"\x05\x01\x00":
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            atyp = header[3]
            if atyp == 1:
                host = socket.inet_ntoa(self._recv_exact(client, 4))
            elif atyp == 3:
                size = self._recv_exact(client, 1)[0]
                host = self._recv_exact(client, size).decode("idna")
            elif atyp == 4:
                self._recv_exact(client, 16)
                self._recv_exact(client, 2)
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            else:
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port = struct.unpack("!H", self._recv_exact(client, 2))[0]
            upstream, _ = _connect_any(self.server.owner.scheduler, host, port)
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            client.settimeout(1)
            _relay(client, upstream, self.server.owner.stop_event)
        except (OSError, ValueError, UnicodeError, ConnectionError):
            try:
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()

    @staticmethod
    def _recv_exact(sock: socket.socket, count: int) -> bytes:
        result = bytearray()
        while len(result) < count:
            chunk = sock.recv(count - len(result))
            if not chunk:
                raise ConnectionError("SOCKS client disconnected during negotiation.")
            result.extend(chunk)
        return bytes(result)


class LocalProxyEngine:
    """Owns loopback proxy listeners, their bounded client threads, and stop signal."""

    def __init__(self, paths: list[AdapterPath]):
        self.scheduler = PathScheduler(paths)
        self.stop_event = threading.Event()
        self._servers = []
        self._threads = []

    def start(self) -> None:
        try:
            for address, handler in (
                ((HTTP_PROXY_HOST, HTTP_PROXY_PORT), _HTTPProxyHandler),
                ((HTTP_PROXY_HOST, SOCKS_PROXY_PORT), _SOCKS5Handler),
            ):
                server = _BoundedThreadingTCPServer(address, handler, self)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                try:
                    thread.start()
                except Exception:
                    server.server_close()
                    raise
                self._servers.append(server)
                self._threads.append(thread)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        self.stop_event.set()
        for server in self._servers:
            try:
                server.shutdown()
            except (OSError, RuntimeError):
                pass
        for server in self._servers:
            try:
                server.server_close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)
        self._servers.clear()
        self._threads.clear()
