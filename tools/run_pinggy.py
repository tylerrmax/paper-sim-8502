"""Run one Pinggy free tunnel session under a pty; record the public URL.
Writes the first run.pinggy-free.link URL seen to ../tunnel_url.txt with a timestamp.
"""
import os, pty, re, select, time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL_FILE = os.path.join(BASE, "tunnel_url.txt")
LOG = open("/tmp/pinggy_tty.log", "ab", buffering=0)
cmd = ["ssh", "-tt", "-p", "443", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "ServerAliveInterval=20", "-o", "ExitOnForwardFailure=yes",
       "-o", f"ProxyCommand=python3 {os.path.join(BASE, 'tools', 'ssh_proxy.py')} %h %p",
       "-R0:127.0.0.1:8502", "free@a.pinggy.io"]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
buf = b""
recorded = False
while True:
    r, _, _ = select.select([fd], [], [], 120)
    if fd in r:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        LOG.write(data)
        if not recorded:
            buf += data
            m = re.search(rb"https://[a-z0-9\-]+\.run\.pinggy-free\.link", buf)
            if m:
                with open(URL_FILE, "w") as f:
                    f.write(m.group(0).decode() + "\n")
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
                recorded = True
