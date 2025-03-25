#!/usr/bin/env python
from __future__ import print_function

import os
import select
import struct
import sys
import time

try:
    import usocket as socket
except ImportError:
    import socket
import platform

# Define to 1 to use builtin "uwebsocket" module of MicroPython
USE_BUILTIN_UWEBSOCKET = 0
# Treat this remote directory as a root for file transfers
SANDBOX = ""
#SANDBOX = "/tmp/webrepl/"
DEBUG = 0

WEBREPL_REQ_S = "<2sBBQLH64s"
WEBREPL_PUT_FILE = 1
WEBREPL_GET_FILE = 2
WEBREPL_GET_VER  = 3
WEBREPL_FRAME_TXT = 0x81
WEBREPL_FRAME_BIN = 0x82
IS_WINDOWS = platform.system() == "Windows"

def debugmsg(msg):
    if DEBUG:
        print(msg)


if USE_BUILTIN_UWEBSOCKET:
    from uwebsocket import websocket
else:
    class websocket:

        def __init__(self, s):
            self.s = s
            self.buf = b""

        def write(self, data, frame=WEBREPL_FRAME_BIN):
            l = len(data)
            if l < 126:
                hdr = struct.pack(">BB", frame, l)
            else:
                hdr = struct.pack(">BBH", frame, 126, l)
            self.s.send(hdr)
            self.s.send(data)

        def recvexactly(self, sz):
            res = b""
            while sz:
                data = self.s.recv(sz)
                if not data:
                    break
                res += data
                sz -= len(data)
            return res

        def read(self, size, text_ok=False):
            if not self.buf:
                while True:
                    hdr = self.recvexactly(2)
                    assert len(hdr) == 2
                    fl, sz = struct.unpack(">BB", hdr)
                    if sz == 126:
                        hdr = self.recvexactly(2)
                        assert len(hdr) == 2
                        (sz,) = struct.unpack(">H", hdr)
                    if fl == 0x82:
                        break
                    if text_ok and fl == 0x81:
                        break
                    debugmsg("Got unexpected websocket record of type %x, skipping it" % fl)
                    while sz:
                        skip = self.s.recv(sz)
                        debugmsg("Skip data: %s" % skip)
                        sz -= len(skip)
                data = self.recvexactly(sz)
                assert len(data) == sz
                self.buf = data

            d = self.buf[:size]
            self.buf = self.buf[size:]
            assert len(d) == size, len(d)
            return d

        def ioctl(self, req, val):
            assert req == 9 and val == 2


def login(ws :websocket, passwd):
    while True:
        c = ws.read(1, text_ok=True)
        if c == b":":
            assert ws.read(1, text_ok=True) == b" "
            break
    ws.write(passwd.encode("utf-8") + b"\r")

def read_resp(ws :websocket):
    data = ws.read(4)
    sig, code = struct.unpack("<2sH", data)
    assert sig == b"WB"
    return code


def send_req(ws : websocket, op, sz=0, fname=b""):
    rec = struct.pack(WEBREPL_REQ_S, b"WA", op, 0, 0, sz, len(fname), fname)
    debugmsg("%r %d" % (rec, len(rec)))
    ws.write(rec)


def get_ver(ws : websocket):
    send_req(ws, WEBREPL_GET_VER)
    d = ws.read(3)
    d = struct.unpack("<BBB", d)
    return d


def do_repl(ws:websocket):
    class ConsoleWindows:
        def __init__(self):
            import msvcrt
            self.msvcrt = msvcrt
            self.infd = sys.stdin.fileno()
            self.infile = sys.stdin.buffer.raw if hasattr(sys.stdin, 'buffer') else sys.stdin
            self.outfile = sys.stdout.buffer.raw if hasattr(sys.stdout, 'buffer') else sys.stdout

        def enter(self):
            # No special terminal setup needed on Windows
            pass

        def exit(self):
            # No special terminal cleanup needed on Windows
            pass

        def readchar(self):
            if self.msvcrt.kbhit():
                return self.msvcrt.getch()
            return None

        def write(self, buf):
            self.outfile.write(buf)

    class ConsolePosix:
        def __init__(self):
            import termios
            self.termios = termios
            self.infd = sys.stdin.fileno()
            self.infile = sys.stdin.buffer.raw
            self.outfile = sys.stdout.buffer.raw
            self.orig_attr = termios.tcgetattr(self.infd)

        def enter(self):
            # attr is: [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
            attr = self.termios.tcgetattr(self.infd)
            attr[0] &= ~(
                self.termios.BRKINT | self.termios.ICRNL | self.termios.INPCK | self.termios.ISTRIP | self.termios.IXON
            )
            attr[1] = 0
            attr[2] = attr[2] & ~(self.termios.CSIZE | self.termios.PARENB) | self.termios.CS8
            attr[3] = 0
            attr[6][self.termios.VMIN] = 1
            attr[6][self.termios.VTIME] = 0
            self.termios.tcsetattr(self.infd, self.termios.TCSANOW, attr)

        def exit(self):
            self.termios.tcsetattr(self.infd, self.termios.TCSANOW, self.orig_attr)

        def readchar(self):
            res = select.select([self.infd], [], [], 0)
            if res[0]:
                return self.infile.read(1)
            else:
                return None

        def write(self, buf):
            self.outfile.write(buf)

    print("Use Ctrl-] to exit this shell")

    console = ConsoleWindows() if IS_WINDOWS else ConsolePosix()
    console.enter()
    try:
        while True:
            if IS_WINDOWS:
                # Windows doesn't support select on stdin, so we poll differently
                c = console.readchar()
                ws_ready = False
                try:
                    # Simple non-blocking check if data available
                    ws_ready = len(select.select([ws.s], [], [], 0)[0]) > 0
                except:
                    pass
            else:
                sel = select.select([console.infd, ws.s], [], [])
                c = console.readchar()
                ws_ready = ws.s in sel[0]

            if c:
                if c == b"\x1d":  # ctrl-], exit
                    break
                else:
                    ws.write(c, WEBREPL_FRAME_TXT)

            if ws_ready:
                c = ws.read(1, text_ok=True)
                while c is not None:
                    # pass character through to the console
                    oc = ord(c)
                    if oc in (8, 9, 10, 13, 27) or oc >= 32:
                        console.write(c)
                    else:
                        console.write(b"[%02x]" % ord(c))
                    if ws.buf:
                        c = ws.read(1)
                    else:
                        c = None
    finally:
        console.exit()


def put_file(ws, local_file, remote_file):
    sz = os.stat(local_file)[6]
    dest_fname = (SANDBOX + remote_file).encode("utf-8")
    rec = struct.pack(WEBREPL_REQ_S, b"WA", WEBREPL_PUT_FILE, 0, 0, sz, len(dest_fname), dest_fname)
    debugmsg("%r %d" % (rec, len(rec)))
    ws.write(rec[:10])
    ws.write(rec[10:])
    assert read_resp(ws) == 0
    cnt = 0
    with open(local_file, "rb") as f:
        while True:
            sys.stdout.write("Sent %d of %d bytes\r" % (cnt, sz))
            sys.stdout.flush()
            buf = f.read(1024)
            if not buf:
                break
            ws.write(buf)
            cnt += len(buf)
    print()
    assert read_resp(ws) == 0

def get_file(ws, local_file, remote_file):
    src_fname = (SANDBOX + remote_file).encode("utf-8")
    rec = struct.pack(WEBREPL_REQ_S, b"WA", WEBREPL_GET_FILE, 0, 0, 0, len(src_fname), src_fname)
    debugmsg("%r %d" % (rec, len(rec)))
    ws.write(rec)
    assert read_resp(ws) == 0
    with open(local_file, "wb") as f:
        cnt = 0
        while True:
            ws.write(b"\0")
            (sz,) = struct.unpack("<H", ws.read(2))
            if sz == 0:
                break
            while sz:
                buf = ws.read(sz)
                if not buf:
                    raise OSError()
                cnt += len(buf)
                f.write(buf)
                sz -= len(buf)
                sys.stdout.write("Received %d bytes\r" % cnt)
                sys.stdout.flush()
    print()
    assert read_resp(ws) == 0


def do_file_upload(ws:websocket, filename, auto_exit=False):
    """Read a file and send its contents line by line to the REPL."""
    print(f"Sending file {filename} to REPL...")
    try:
        # TODO:  enter raw / raw-paste mode
        
        # Read the file and send its contents line by line
        with open(filename, "r") as f:
            for line in f:
                ws.write(line.encode('utf-8'), WEBREPL_FRAME_TXT)
                ws.write(b"\r", WEBREPL_FRAME_TXT)
                time.sleep(0.2)  # Small , but large enough delay to allow processing of each line

        # TODO:  exit raw / raw-paste mode
        
        # Wait for processing after sending file
        time.sleep(5)
        
        # Display output from device after file is sent
        ws_ready = False
        try:
            # Check if data available from WebREPL
            ws_ready = len(select.select([ws.s], [], [], 0.2)[0]) > 0
        except:
            pass
        
        if ws_ready:
            c = ws.read(1, text_ok=True)
            while c is not None:
                oc = ord(c)
                if oc in {8, 9, 10, 13, 27} or oc >= 32:
                    sys.stdout.buffer.write(c)
                else:
                    sys.stdout.buffer.write(b"[%02x]" % ord(c))
                sys.stdout.flush()
                c = ws.read(1) if ws.buf else None
                
        # Wait for a final 200ms after file transfer is complete
        if auto_exit:
            time.sleep(0.2)
            # Send Ctrl-] to exit the REPL if auto_exit is True
            ws.write(b"\x1d", WEBREPL_FRAME_TXT)

        print(f"\nFile {filename} sent successfully.")
        return True
    except Exception as e:
        print(f"Error sending file: {e}")
        return False

def help(rc=0):
    exename = sys.argv[0].rsplit("/", 1)[-1]
    print(
        "%s - Access REPL, perform remote file operations via MicroPython WebREPL protocol"
        % exename
    )
    print("Arguments:")
    print("  [-p password] [-f file_to_upload] <host>        - Access the remote REPL")
    print("  [-p password] <host>:<remote_file> <local_file> - Copy remote file to local file")
    print("  [-p password] <local_file> <host>:<remote_file> - Copy local file to remote file")
    print("Examples:")
    print("  %s 192.168.4.1" % exename)
    print("  %s -f script.py 192.168.4.1" % exename)
    print("  %s script.py 192.168.4.1:/another_name.py" % exename)
    print("  %s script.py 192.168.4.1:/app/" % exename)
    print("  %s -p password 192.168.4.1:/app/script.py ." % exename)
    sys.exit(rc)

def error(msg):
    print(msg)
    sys.exit(1)

def parse_remote(remote):
    host, fname = remote.rsplit(":", 1)
    if fname == "":
        fname = "/"
    port = 8266
    if ":" in host:
        host, port = host.split(":")
        port = int(port)
    return (host, port, fname)


# Very simplified client handshake, works for MicroPython's
# websocket server implementation, but probably not for other
# servers.
def client_handshake(sock):
    cl = sock.makefile("rwb", 0)
    cl.write(b"""\
GET / HTTP/1.1\r
Host: echo.websocket.org\r
Connection: Upgrade\r
Upgrade: websocket\r
Sec-WebSocket-Key: foo\r
\r
""")
    l = cl.readline()
#    print(l)
    while 1:
        l = cl.readline()
        if l == b"\r\n":
            break
#        sys.stdout.write(l)


def main():
    passwd = None
    upload_file = None
    stay_in_repl = True
    
    i = 0
    while i < len(sys.argv):
        if sys.argv[i] == '-p':
            sys.argv.pop(i)
            passwd = sys.argv.pop(i)
        elif sys.argv[i] == '-f':
            sys.argv.pop(i)
            upload_file = sys.argv.pop(i)
            # If -f option is used, default to not staying in REPL
            stay_in_repl = False
        else:
            i += 1

    if len(sys.argv) not in (2, 3):
        help(1)

    if passwd is None:
        import getpass
        passwd = getpass.getpass()

    if len(sys.argv) > 2:
        if ":" in sys.argv[1] and ":" in sys.argv[2]:
            error("Operations on 2 remote files are not supported")
        if ":" not in sys.argv[1] and ":" not in sys.argv[2]:
            error("One remote file is required")

    if len(sys.argv) == 2:
        op = "repl"
        host, port, _ = parse_remote(sys.argv[1] + ":")
    elif ":" in sys.argv[1]:
        op = "get"
        host, port, src_file = parse_remote(sys.argv[1])
        dst_file = sys.argv[2]
        if os.path.isdir(dst_file):
            basename = src_file.rsplit("/", 1)[-1]
            dst_file += "/" + basename
    else:
        op = "put"
        host, port, dst_file = parse_remote(sys.argv[2])
        src_file = sys.argv[1]
        if dst_file[-1] == "/":
            basename = src_file.rsplit("/", 1)[-1]
            dst_file += basename

    if True:
        print("op:%s, host:%s, port:%d, passwd:%s." % (op, host, port, passwd))
        if op in ("get", "put"):
            print(src_file, "->", dst_file)

    s = socket.socket()

    ai = socket.getaddrinfo(host, port)
    addr = ai[0][4]

    s.connect(addr)
    #s = s.makefile("rwb")
    client_handshake(s)

    ws : websocket = websocket(s)

    login(ws, passwd)
    print("Remote WebREPL version:", get_ver(ws))

    # Set websocket to send data marked as "binary"
    ws.ioctl(9, 2)

    if op == "repl":
        if upload_file:
            # Upload file with auto-exit flag set
            do_file_upload(ws, upload_file, auto_exit=not stay_in_repl)
            
            if not stay_in_repl:
                # Auto-exit has already been handled in do_file_upload
                print("Auto-exiting REPL session after file upload.")
                return
        
        # If we're still here, enter the interactive REPL
        do_repl(ws)
    elif op == "get":
        get_file(ws, dst_file, src_file)
    elif op == "put":
        put_file(ws, src_file, dst_file)

    s.close()


if __name__ == "__main__":
    main()
