"""Fixed loopback/Unix-socket forwarding; never executes application code."""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket

parser = argparse.ArgumentParser()
parser.add_argument('kind', choices=['api', 'web'])
parser.add_argument('--root', required=True)
parser.add_argument('--port', type=int, required=True)
args = parser.parse_args()
deployment_root = Path(args.root).absolute()
assert deployment_root.resolve() == deployment_root
root = deployment_root / 'var'
assert root.resolve() == root
assert args.port not in {3000, 3015, 8175, 8185}
socket_path = root / (args.kind + '.sock')
port = args.port
hop = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'}

class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(120)
        self.sock.connect(str(socket_path))

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def forward(self):
        if self.headers.get('Transfer-Encoding'):
            self.send_error(411, 'Use Content-Length')
            return
        upstream = Connection('localhost', timeout=120)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            headers = {key: value for key, value in self.headers.items() if key.lower() not in hop}
            upstream.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for key, value in headers.items():
                upstream.putheader(key, value)
            upstream.endheaders()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    raise ConnectionError('Incomplete request body')
                upstream.send(chunk)
                remaining -= len(chunk)
            response = upstream.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in hop:
                    self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD':
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError, http.client.HTTPException):
            self.send_error(502, 'Development upstream unavailable')
        finally:
            upstream.close()
            self.close_connection = True
    do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = forward

ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
