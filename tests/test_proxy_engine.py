from __future__ import annotations

from pathlib import Path
import socket
import socketserver
import struct
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy_engine import AdapterPath, LocalProxyEngine, PathScheduler  # noqa: E402


class HTTPOrigin(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(16 * 1024)
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
        )


class EchoOrigin(socketserver.BaseRequestHandler):
    def handle(self):
        payload = bytearray()
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            payload.extend(chunk)
        self.request.sendall(payload)


class TestServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def read_until(sock: socket.socket, marker: bytes) -> bytes:
    result = bytearray()
    while marker not in result:
        chunk = sock.recv(4096)
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


class PathSchedulerTests(unittest.TestCase):
    def test_same_host_stays_on_one_path_and_new_hosts_are_distributed(self):
        first = AdapterPath("Ethernet", 18, "192.0.2.10", "192.0.2.1")
        second = AdapterPath("Wi-Fi", 6, "198.51.100.10", "198.51.100.1")
        scheduler = PathScheduler([first, second])

        self.assertEqual(scheduler.ordered_paths("speedtest.net"), (first, second))
        self.assertEqual(scheduler.ordered_paths("SPEEDTEST.NET."), (first, second))
        self.assertEqual(scheduler.ordered_paths("another-site.example"), (second, first))
        self.assertEqual(scheduler.ordered_paths("speedtest.net"), (first, second))

    def test_requires_two_paths(self):
        path = AdapterPath("Ethernet", 18, "192.0.2.10", "192.0.2.1")
        with self.assertRaisesRegex(ValueError, "Connect Ethernet and Wi-Fi"):
            PathScheduler([path])


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http_origin = TestServer(("127.0.0.1", 0), HTTPOrigin)
        cls.echo_origin = TestServer(("127.0.0.1", 0), EchoOrigin)
        cls.origin_threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (cls.http_origin, cls.echo_origin)
        ]
        for thread in cls.origin_threads:
            thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.http_origin, cls.echo_origin):
            server.shutdown()
            server.server_close()

    def setUp(self):
        loopback = AdapterPath("loopback test", 1, "127.0.0.1", "127.0.0.1")
        self.engine = LocalProxyEngine(
            [loopback, loopback],
            http_address=("127.0.0.1", 0),
            socks_address=("127.0.0.1", 0),
        )
        self.engine.start()
        self.http_address, self.socks_address = self.engine.addresses

    def tearDown(self):
        self.engine.stop()

    def test_http_forwarding(self):
        with socket.create_connection(self.http_address, timeout=5) as client:
            client.settimeout(5)
            port = self.http_origin.server_address[1]
            client.sendall(
                f"GET http://127.0.0.1:{port}/test?q=1 HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n\r\n".encode("ascii")
            )
            response = read_until(client, b"\r\n\r\n")
            self.assertIn(b"200 OK", response)
            response += client.recv(16)
            self.assertTrue(response.endswith(b"OK"), response)

    def test_https_connect_relay_supports_client_half_close(self):
        with socket.create_connection(self.http_address, timeout=5) as client:
            client.settimeout(5)
            port = self.echo_origin.server_address[1]
            client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
            response = read_until(client, b"\r\n\r\n")
            self.assertIn(b"200 Connection established", response)
            client.sendall(b"half-close-check")
            client.shutdown(socket.SHUT_WR)
            echoed = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                echoed.extend(chunk)
            self.assertEqual(bytes(echoed), b"half-close-check")

    def test_socks5_domain_tunnel(self):
        with socket.create_connection(self.socks_address, timeout=5) as client:
            client.settimeout(5)
            client.sendall(b"\x05\x01\x00")
            self.assertEqual(client.recv(2), b"\x05\x00")
            host = b"localhost"
            port = self.echo_origin.server_address[1]
            client.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", port))
            reply = read_until(client, b"\x00\x00\x00\x00\x00\x00")
            self.assertTrue(reply.startswith(b"\x05\x00"), reply)
            client.sendall(b"socks-check")
            client.shutdown(socket.SHUT_WR)
            echoed = bytearray()
            while True:
                chunk = client.recv(32)
                if not chunk:
                    break
                echoed.extend(chunk)
            self.assertEqual(bytes(echoed), b"socks-check")

    def test_stopping_closes_idle_client_and_listeners(self):
        client = socket.create_connection(self.http_address, timeout=5)
        client.settimeout(3)
        try:
            self.engine.stop()
            self.assertEqual(client.recv(1), b"")
            with self.assertRaises(OSError):
                socket.create_connection(self.http_address, timeout=0.5)
        finally:
            client.close()

    def test_engine_instance_cannot_be_restarted(self):
        self.engine.stop()
        with self.assertRaisesRegex(RuntimeError, "only be started once"):
            self.engine.start()


if __name__ == "__main__":
    unittest.main()
