import socket, sys, time

ip = sys.argv[1] if len(sys.argv) > 1 else "<手机IP>"
ports = list(range(1, 1024)) + list(range(1024, 65536, 1))
found = []
BATCH = 3000

def probe(p):
    s = socket.socket()
    s.settimeout(1.2)
    try:
        s.connect((ip, p))
        return True
    except Exception:
        return False
    finally:
        s.close()

for i in range(0, len(ports), BATCH):
    chunk = ports[i:i + BATCH]
    socks = []
    for p in chunk:
        s = socket.socket()
        s.setblocking(False)
        try:
            s.connect_ex((ip, p))
            socks.append((p, s))
        except Exception:
            pass
    time.sleep(1.4)
    for p, s in socks:
        try:
            err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err == 0:
                found.append(p)
        except Exception:
            pass
        s.close()
    print("scanned %d/%d, open=%s" % (min(i + BATCH, len(ports)), len(ports), found), flush=True)

print("OPEN PORTS:", found, flush=True)
