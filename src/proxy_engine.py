"""Loopback HTTP/HTTPS and SOCKS5 proxies with per-connection NIC selection."""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from http import HTTPStatus
import selectors
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
MAX_RELAY_BUFFER = 256 * 1024
IP_UNICAST_IF = getattr(socket, "IP_UNICAST_IF", 31)


@dataclass(frozen=True)
class AdapterPath:
    name: str
    interface_index: int
    ipv4_address: str
    gateway: str


class PathScheduler:
    """Keep each destination host on one link; distribute new hosts in turn.

    Browsers open many parallel connections to one site. Sending each one over
    a different ISP can make the site's latency/download phases appear to come
    from different public addresses and can break session checks. Host affinity
    avoids that while still distributing separate hosts across available links.
    """

    MAX_AFFINITY_HOSTS = 4096

    def __init__(self, paths: list[AdapterPath]):
        if len(paths) < 2:
            raise ValueError("Connect Ethernet and Wi-Fi before starting Boost.")
        self._paths = tuple(paths)
        self._index = 0
        self._host_paths: OrderedDict[str, AdapterPath] = OrderedDict()
        self._lock = threading.Lock()

    def ordered_paths(self, host: str) -> tuple[AdapterPath, ...]:
        key = host.rstrip(".").lower()
        with self._lock:
            preferred = self._host_paths.get(key)
            if preferred is None:
                preferred = self._paths[self._index]
                self._index = (self._index + 1) % len(self._paths)
                self._host_paths[key] = preferred
                if len(self._host_paths) > self.MAX_AFFINITY_HOSTS:
                    self._host_paths.popitem(last=False)
            else:
                self._host_paths.move_to_end(key)
            start = self._paths.index(preferred)
        return self._paths[start:] + self._paths[:start]


def _open_on_path(path: AdapterPath, host: str, port: int) -> socket.socket:
    """Open an IPv4 TCP connection pinned to an adapter and source address."""
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
            # Relay uses a readiness selector. Leaving the 10-second connect
            # timeout on the socket would abort otherwise healthy long transfers.
            sock.settimeout(None)
            return sock
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            sock.close()
    raise OSError("; ".join(errors) or "No IPv4 address was returned for the host.")


def _connect_any(owner: "LocalProxyEngine", host: str, port: int) -> tuple[socket.socket, AdapterPath]:
    errors = []
    for path in owner.scheduler.ordered_paths(host):
        try:
            upstream = _open_on_path(path, host, port)
            if not owner.track_socket(upstream):
                raise OSError("Boost stopped while opening the connection.")
            return upstream, path
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
    end = data.index(b"\r\n\r\n") + 4
    return bytes(data[:end]), bytes(data[end:])


def _send_http_error(sock: socket.socket, status: HTTPStatus, detail: str) -> None:
    body = (detail + "\n").encode("utf-8", errors="replace")
    response = (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
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
        raise ValueError("IPv6 literal destinations are not supported by this build.")
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


def _relay(
    client: socket.socket,
    upstream: socket.socket,
    stop_event: threading.Event,
    initial_to_upstream: bytes = b"",
) -> None:
    """Relay with bounded buffers, back-pressure, and TCP half-close support."""
    try:
        client.setblocking(False)
        upstream.setblocking(False)
        with selectors.DefaultSelector() as selector:
            peers = {client: upstream, upstream: client}
            buffers = {client: bytearray(), upstream: bytearray()}
            if initial_to_upstream:
                buffers[upstream].extend(initial_to_upstream)
            read_open = {client, upstream}
            write_shutdown = set()

            def update(sock: socket.socket) -> None:
                events = 0
                peer = peers[sock]
                if sock in read_open and len(buffers[peer]) < MAX_RELAY_BUFFER:
                    events |= selectors.EVENT_READ
                if buffers[sock]:
                    events |= selectors.EVENT_WRITE
                try:
                    selector.get_key(sock)
                    if events:
                        selector.modify(sock, events)
                    else:
                        selector.unregister(sock)
                except KeyError:
                    if events:
                        selector.register(sock, events)

            update(client)
            update(upstream)
            while not stop_event.is_set():
                if not read_open and not any(buffers.values()):
                    return
                try:
                    events = selector.select(timeout=0.25)
                except (OSError, ValueError):
                    return
                for key, mask in events:
                    source = key.fileobj
                    peer = peers[source]
                    if mask & selectors.EVENT_READ:
                        try:
                            chunk = source.recv(min(64 * 1024, MAX_RELAY_BUFFER - len(buffers[peer])))
                        except BlockingIOError:
                            chunk = None
                        except OSError:
                            return
                        if chunk is None:
                            pass
                        elif chunk:
                            buffers[peer].extend(chunk)
                        else:
                            read_open.discard(source)
                            if not buffers[peer] and peer not in write_shutdown:
                                try:
                                    peer.shutdown(socket.SHUT_WR)
                                except OSError:
                                    pass
                                write_shutdown.add(peer)
                    if mask & selectors.EVENT_WRITE and buffers[source]:
                        try:
                            sent = source.send(buffers[source])
                        except BlockingIOError:
                            sent = 0
                        except OSError:
                            return
                        if sent:
                            del buffers[source][:sent]
                        if not buffers[source] and peer not in read_open and source not in write_shutdown:
                            try:
                                source.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass
                            write_shutdown.add(source)
                    update(source)
                    update(peer)
    except (OSError, ValueError):
        return


class _BoundedThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, owner):
        self.owner = owner
        super().__init__(address, handler, bind_and_activate=True)

    def process_request(self, request, client_address):
        if not self.owner.acquire_client_slot():
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.owner.release_client_slot()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.owner.release_client_slot()


class _HTTPProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        owner = self.server.owner
        upstream = None
        if not owner.track_socket(client):
            return
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
                upstream, path = _connect_any(owner, host, port)
                client.sendall(
                    f"{version} 200 Connection established\r\n"
                    f"X-NetFusion-Adapter: {path.name}\r\n\r\n".encode("latin-1")
                )
                _relay(client, upstream, owner.stop_event, initial_body)
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
                raise ValueError("IPv6 literal destinations are not supported by this build.")
            port = parsed.port or 80
            if not 1 <= port <= 65535:
                raise ValueError("Invalid destination port.")
            upstream, _ = _connect_any(owner, host, port)
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
            _relay(client, upstream, owner.stop_event)
        except (OSError, ValueError, UnicodeError, ConnectionError) as exc:
            _send_http_error(client, HTTPStatus.BAD_GATEWAY, f"NetFusion could not open this connection: {exc}")
        finally:
            if upstream is not None:
                owner.untrack_socket(upstream)
                upstream.close()
            owner.untrack_socket(client)


class _SOCKS5Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        owner = self.server.owner
        upstream = None
        if not owner.track_socket(client):
            return
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
            upstream, _ = _connect_any(owner, host, port)
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            _relay(client, upstream, owner.stop_event)
        except (OSError, ValueError, UnicodeError, ConnectionError):
            try:
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
        finally:
            if upstream is not None:
                owner.untrack_socket(upstream)
                upstream.close()
            owner.untrack_socket(client)

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
    """Owns loopback listeners and enforces single-start, bounded clients, and cleanup."""

    def __init__(
        self,
        paths: list[AdapterPath],
        http_address=(HTTP_PROXY_HOST, HTTP_PROXY_PORT),
        socks_address=(HTTP_PROXY_HOST, SOCKS_PROXY_PORT),
    ):
        self.scheduler = PathScheduler(paths)
        self.stop_event = threading.Event()
        self._http_address = http_address
        self._socks_address = socks_address
        self._servers = []
        self._threads = []
        self._state_lock = threading.Lock()
        self._state = "new"
        self._active_lock = threading.Lock()
        self._active_sockets = set()
        self._client_slots = threading.BoundedSemaphore(MAX_CLIENTS)

    @property
    def addresses(self) -> tuple[tuple[str, int], tuple[str, int]]:
        http = self._servers[0].server_address if self._servers else self._http_address
        socks = self._servers[1].server_address if len(self._servers) > 1 else self._socks_address
        return http, socks

    def acquire_client_slot(self) -> bool:
        return self._client_slots.acquire(blocking=False)

    def release_client_slot(self) -> None:
        self._client_slots.release()

    def track_socket(self, sock: socket.socket) -> bool:
        with self._active_lock:
            if self.stop_event.is_set():
                accepted = False
            else:
                self._active_sockets.add(sock)
                accepted = True
        if not accepted:
            try:
                sock.close()
            except OSError:
                pass
        return accepted

    def untrack_socket(self, sock: socket.socket) -> None:
        with self._active_lock:
            self._active_sockets.discard(sock)

    def start(self) -> None:
        with self._state_lock:
            if self._state != "new":
                raise RuntimeError("A proxy engine instance can only be started once.")
            self._state = "starting"
        try:
            for address, handler in (
                (self._http_address, _HTTPProxyHandler),
                (self._socks_address, _SOCKS5Handler),
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
        with self._state_lock:
            self._state = "running"

    def stop(self) -> None:
        with self._state_lock:
            if self._state == "stopped":
                return
            self._state = "stopping"
            self.stop_event.set()
        with self._active_lock:
            active = tuple(self._active_sockets)
        for sock in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
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
        with self._state_lock:
            self._state = "stopped"
