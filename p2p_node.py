"""
p2p_node.py - The P2P networking layer.

Every peer is BOTH a TCP server and a TCP client:

  * Server role: a listener thread runs socket() -> bind() -> listen() ->
    accept() and hands every accepted connection to its own thread.
  * Client role: connect() opens a TCP connection to another peer.

Once a TCP connection exists (in either direction) the two peers perform a
HELLO / HELLO_ACK handshake so each side learns the other's id, name and
listening port. After that the connection is symmetric: both sides can
send text messages and files at any time.

The node never touches the GUI directly. It reports everything through a
single callback, `on_event(kind, data)`, so it can be used from Tkinter,
from a command-line program, or from tests.
"""

import os
import socket
import threading
import uuid
import ipaddress

import protocol

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
CONNECT_TIMEOUT = 5      # seconds to wait for connect() and the handshake


class PeerConnection:
    """One live TCP connection to a remote peer."""

    def __init__(self, sock, address, peer_id, peer_name, listen_port):
        self.sock = sock
        self.address = address            # (ip, port) of the TCP connection
        self.peer_id = peer_id
        self.peer_name = peer_name
        self.listen_port = listen_port    # port the remote peer listens on
        # Only one thread may write to a socket at a time; otherwise the
        # bytes of a file and a text message could get mixed together.
        self.send_lock = threading.Lock()

    def label(self):
        return f"{self.peer_name} [{self.peer_id}] {self.address[0]}:{self.listen_port}"


class P2PNode:
    def __init__(self, on_event=None):
        # on_event(kind, data) where kind is one of:
        #   "system", "error", "text_in", "text_out",
        #   "file_in", "file_out", "progress", "peers"
        self.on_event = on_event or (lambda kind, data: None)

        self.peer_id = None
        self.name = None
        self.port = None
        self.server_socket = None
        self.running = False

        self.peers = {}                 # peer_id -> PeerConnection
        self.peers_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, kind, data=None):
        try:
            self.on_event(kind, data)
        except Exception:
            pass   # a broken UI callback must never kill a network thread

    def get_peers(self):
        """Return a snapshot list of the connected peers."""
        with self.peers_lock:
            return list(self.peers.values())

    # ------------------------------------------------------------------
    # Server role
    # ------------------------------------------------------------------

    def start(self, name, port):
        """Start listening for incoming connections. Raises ValueError/OSError on failure."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Peer name cannot be empty")
        port = validate_port(port)
        if self.running:
            raise ValueError("Peer is already running")

        # socket() -> bind() -> listen()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: make bind() fail if another peer already uses this port
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Linux/macOS: allow quick restart on the same port after Stop
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", port))   # 0.0.0.0 = accept on every network interface
            server.listen()
        except OSError:
            server.close()
            raise

        self.server_socket = server
        self.name = name
        self.port = port
        self.peer_id = uuid.uuid4().hex[:8]     # short random id, e.g. "a83f21c4"
        self.running = True
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        threading.Thread(target=self._accept_loop, daemon=True).start()
        self._emit("system", f"Peer started: {self.name} [{self.peer_id}] on port {self.port}")

    def _accept_loop(self):
        """while server_is_running: connection = accept(); create_new_thread(connection)"""
        while self.running:
            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                break   # server socket was closed by stop()
            threading.Thread(
                target=self._handle_incoming, args=(conn, addr), daemon=True
            ).start()

    def _handle_incoming(self, sock, addr):
        """Handshake for a connection that another peer opened to us."""
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            hello = protocol.recv_message(sock)
            if hello is None or hello.get("type") != protocol.HELLO:
                raise protocol.ProtocolError("Expected HELLO message")

            protocol.send_message(
                sock, protocol.make_hello_ack(self.peer_id, self.name, self.port)
            )
            sock.settimeout(None)
            peer = PeerConnection(
                sock, addr, str(hello.get("peer_id")),
                str(hello.get("peer_name")), hello.get("port"),
            )
        except (OSError, protocol.ProtocolError) as e:
            self._emit("error", f"Handshake with {addr[0]}:{addr[1]} failed: {e}")
            sock.close()
            return

        if self._register_peer(peer):
            self._emit("system", f"Connected to {peer.peer_name} (incoming from {addr[0]}:{addr[1]})")
            self._receive_loop(peer)

    # ------------------------------------------------------------------
    # Client role
    # ------------------------------------------------------------------

    def connect(self, ip, port):
        """Open a TCP connection to another peer and perform the handshake."""
        if not self.running:
            raise ValueError("Start your peer first")
        ip = validate_ip(ip)
        port = validate_port(port)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((ip, port))
            protocol.send_message(sock, protocol.make_hello(self.peer_id, self.name, self.port))
            ack = protocol.recv_message(sock)
            if ack is None or ack.get("type") != protocol.HELLO_ACK:
                raise protocol.ProtocolError("Expected HELLO_ACK message")
            sock.settimeout(None)
        except ConnectionRefusedError:
            sock.close()
            raise ConnectionError("Connection refused (is the peer running on that port?)")
        except socket.timeout:
            sock.close()
            raise ConnectionError("Connection timed out")
        except (OSError, protocol.ProtocolError) as e:
            sock.close()
            raise ConnectionError(str(e))

        peer = PeerConnection(
            sock, (ip, port), str(ack.get("peer_id")), str(ack.get("peer_name")), ack.get("port")
        )
        if not self._register_peer(peer):
            raise ConnectionError(f"Already connected to {peer.peer_name} (or it is yourself)")

        self._emit("system", f"Connected to {peer.peer_name} ({ip}:{port})")
        threading.Thread(target=self._receive_loop, args=(peer,), daemon=True).start()

    # ------------------------------------------------------------------
    # Peer bookkeeping
    # ------------------------------------------------------------------

    def _register_peer(self, peer):
        """Add a peer after a successful handshake. Rejects duplicates and self-connections."""
        with self.peers_lock:
            duplicate = peer.peer_id == self.peer_id or peer.peer_id in self.peers
            if not duplicate:
                self.peers[peer.peer_id] = peer
        if duplicate:
            peer.sock.close()
            return False
        self._emit("peers", self.get_peers())
        return True

    def _remove_peer(self, peer, reason=None):
        with self.peers_lock:
            removed = self.peers.get(peer.peer_id) is peer
            if removed:
                del self.peers[peer.peer_id]
        try:
            peer.sock.close()
        except OSError:
            pass
        if removed:
            msg = f"{peer.peer_name} disconnected"
            if reason:
                msg += f" ({reason})"
            self._emit("system", msg)
            self._emit("peers", self.get_peers())

    def disconnect(self, peer_id):
        peer = self.peers.get(peer_id)
        if peer:
            self._remove_peer(peer, "closed by you")

    def stop(self):
        """Stop the server and close every connection."""
        if not self.running:
            return
        self.running = False
        try:
            self.server_socket.close()
        except OSError:
            pass
        for peer in self.get_peers():
            self._remove_peer(peer)
        self._emit("system", "Peer stopped")

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    def _receive_loop(self, peer):
        """Runs in its own thread: read messages from one peer until it disconnects."""
        reason = None
        try:
            while self.running:
                message = protocol.recv_message(peer.sock)
                if message is None:
                    break                      # peer closed the connection
                mtype = message.get("type")
                if mtype == protocol.TEXT:
                    self._emit("text_in", (peer, str(message.get("message", ""))))
                elif mtype == protocol.FILE:
                    self._receive_file(peer, message)
                else:
                    self._emit("error", f"Unknown message type from {peer.peer_name}: {mtype}")
        except (OSError, protocol.ProtocolError) as e:
            if self.running:
                reason = str(e)
        finally:
            self._remove_peer(peer, reason)

    def _receive_file(self, peer, meta):
        """
        Read exactly `filesize` raw bytes that follow the file metadata and
        write them to downloads/. We never assume one recv() == one file.
        """
        filename = safe_filename(meta.get("filename"))
        filesize = meta.get("filesize")
        if not isinstance(filesize, int) or filesize < 0:
            # We cannot know how many bytes to skip, so the stream is now unusable.
            raise protocol.ProtocolError(f"Invalid file size: {filesize!r}")

        path = unique_path(os.path.join(DOWNLOADS_DIR, filename))
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        remaining = filesize
        try:
            with open(path, "wb") as f:
                while remaining > 0:
                    chunk = peer.sock.recv(min(protocol.CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ConnectionError("Connection closed during file transfer")
                    f.write(chunk)
                    remaining -= len(chunk)
                    self._emit("progress", (f"Receiving {filename}", filesize - remaining, filesize))
        except BaseException:
            # Do not leave a half-written file behind
            try:
                os.remove(path)
            except OSError:
                pass
            raise

        self._emit("file_in", (peer, os.path.basename(path), path, filesize))

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _get_peer(self, peer_id):
        if not peer_id:
            raise ValueError("No peer selected")
        with self.peers_lock:
            peer = self.peers.get(peer_id)
        if peer is None:
            raise ValueError("Selected peer is no longer connected")
        return peer

    def send_text(self, peer_id, text):
        text = (text or "").strip()
        if not text:
            raise ValueError("Message is empty")
        peer = self._get_peer(peer_id)
        try:
            with peer.send_lock:
                protocol.send_message(peer.sock, protocol.make_text(self.peer_id, self.name, text))
        except OSError as e:
            self._remove_peer(peer, str(e))
            raise ConnectionError(f"Could not send to {peer.peer_name}: {e}")
        self._emit("text_out", (peer, text))

    def send_file(self, peer_id, filepath):
        """Send metadata, then the raw bytes in 64 KB chunks. Blocks until done."""
        peer = self._get_peer(peer_id)
        if not filepath or not os.path.isfile(filepath):
            raise FileNotFoundError(f"File does not exist: {filepath}")

        filename = os.path.basename(filepath)
        f = open(filepath, "rb")        # may raise OSError (e.g. permission denied)
        try:
            filesize = os.fstat(f.fileno()).st_size
            sent = 0
            # Hold the lock for the whole transfer so no other message can
            # be inserted between the metadata and the file bytes.
            with peer.send_lock:
                protocol.send_message(
                    peer.sock, protocol.make_file(self.peer_id, self.name, filename, filesize)
                )
                while sent < filesize:
                    chunk = f.read(min(protocol.CHUNK_SIZE, filesize - sent))
                    if not chunk:
                        # File shrank while sending; the receiver still expects
                        # more bytes, so the stream is out of sync.
                        raise OSError("File changed during transfer")
                    peer.sock.sendall(chunk)
                    sent += len(chunk)
                    self._emit("progress", (f"Sending {filename}", sent, filesize))
        except OSError as e:
            # Part of the file may already be on the wire, so this
            # connection can no longer be trusted: drop it.
            self._remove_peer(peer, str(e))
            raise ConnectionError(f"File transfer to {peer.peer_name} failed: {e}")
        finally:
            f.close()
        self._emit("file_out", (peer, filename, filesize))

    def send_file_async(self, peer_id, filepath):
        """Send a file in a background thread so the UI stays responsive."""
        # Validate up-front so the user gets an immediate error message.
        self._get_peer(peer_id)
        if not filepath or not os.path.isfile(filepath):
            raise FileNotFoundError(f"File does not exist: {filepath}")

        def worker():
            try:
                self.send_file(peer_id, filepath)
            except Exception as e:
                self._emit("error", str(e))

        threading.Thread(target=worker, daemon=True).start()


# ----------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------

def validate_port(port):
    try:
        port = int(str(port).strip())
    except ValueError:
        raise ValueError(f"Invalid port: {port!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid port: {port} (must be 1-65535)")
    return port


def validate_ip(ip):
    ip = (ip or "").strip()
    if ip.lower() == "localhost":
        return "127.0.0.1"
    try:
        return str(ipaddress.IPv4Address(ip))
    except ValueError:
        raise ValueError(f"Invalid IP address: {ip!r}")


def safe_filename(name):
    """Strip any directory parts so a peer cannot write outside downloads/."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = name.strip().strip(".")
    return name or "received_file"


def unique_path(path):
    """photo.jpg -> photo (1).jpg if photo.jpg already exists."""
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(path):
        path = f"{base} ({counter}){ext}"
        counter += 1
    return path
