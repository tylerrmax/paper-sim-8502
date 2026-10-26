"""SSH ProxyCommand helper: CONNECT through the runtime egress proxy.
Reads proxy URL from env HTTPS_PROXY, else /etc/environment.d/80-hatch-egress.conf,
else falls back to hatch-egress-proxy:3128 without auth (auth optional on this net).
"""
import os, sys, socket, urllib.parse, base64, select

def load_proxy():
    url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not url:
        try:
            for line in open("/etc/environment.d/80-hatch-egress.conf"):
                if "PROXY" in line and "=" in line:
                    url = line.split("=", 1)[1].strip().strip('"')
                    if url.startswith("http"):
                        break
        except Exception:
            pass
    if not url:
        return ("hatch-egress-proxy", 3128, None)
    px = urllib.parse.urlparse(url)
    auth = None
    if px.username:
        auth = base64.b64encode(f"{urllib.parse.unquote(px.username)}:{urllib.parse.unquote(px.password or '')}".encode()).decode()
    return (px.hostname, px.port or 3128, auth)

def main():
    host, port = sys.argv[1], int(sys.argv[2])
    ph, pp, auth = load_proxy()
    s = socket.create_connection((ph, pp), timeout=30)
    hdr = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if auth:
        hdr += f"Proxy-Authorization: Basic {auth}\r\n"
    s.sendall((hdr + "\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            sys.exit("proxy closed during CONNECT")
        buf += chunk
    if " 200 " not in buf.split(b"\r\n")[0].decode(errors="replace"):
        sys.exit("CONNECT refused: " + buf.split(b"\r\n")[0].decode(errors="replace"))
    leftover = buf.split(b"\r\n\r\n", 1)[1]
    if leftover:
        sys.stdout.buffer.write(leftover); sys.stdout.buffer.flush()
    s.settimeout(None)
    fds = [s, sys.stdin.buffer]
    while True:
        r, _, _ = select.select(fds, [], [], 120)
        if not r:
            continue
        for f in r:
            if f is s:
                data = s.recv(65536)
                if not data:
                    return
                sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
            else:
                data = os.read(0, 65536)
                if not data:
                    s.shutdown(socket.SHUT_WR); return
                s.sendall(data)

main()
