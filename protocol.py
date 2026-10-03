"""
protocol.py - Application-level protocol for the P2P network.

Every control message is a JSON object sent with a simple framing scheme:

    +----------------+----------------------+
    | 4-byte length  |     JSON payload     |
    +----------------+----------------------+

The length is an unsigned 32-bit big-endian integer ("network byte order")
that tells the receiver exactly how many bytes of JSON follow. This is
needed because TCP is a byte stream: one recv() call may return half a
message, or two messages glued together.

Message types:
    hello      - sent by the peer that opens the connection (handshake)
    hello_ack  - reply to hello (handshake)
    text       - a chat message
    file       - file metadata; followed immediately by `filesize` raw bytes
"""

import json
import struct

# Message type constants
HELLO = "hello"
HELLO_ACK = "hello_ack"
TEXT = "text"
FILE = "file"

HEADER_SIZE = 4                    # 4-byte length prefix
CHUNK_SIZE = 64 * 1024             # 64 KB chunks for file transfer
MAX_MESSAGE_SIZE = 1024 * 1024     # 1 MB limit for a single JSON message


class ProtocolError(Exception):
    """Raised when the other peer sends data that breaks the protocol."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def recv_exact(sock, n):
    """
    Read exactly n bytes from the socket.

    A single recv() may return fewer bytes than requested, so we keep
    reading until we have all n bytes. Returns None if the connection
    was closed before n bytes arrived.
    """
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:          # empty bytes => the peer closed the connection
            return None
        data.extend(chunk)
    return bytes(data)


def send_message(sock, message):
    """Encode a dict as JSON and send it with a 4-byte length prefix."""
    payload = json.dumps(message).encode("utf-8")
    header = struct.pack("!I", len(payload))   # !I = big-endian unsigned int
    sock.sendall(header + payload)


def recv_message(sock):
    """
    Receive one framed JSON message.

    Returns the decoded dict, or None if the connection was closed.
    Raises ProtocolError if the data is not a valid message.
    """
    header = recv_exact(sock, HEADER_SIZE)
    if header is None:
        return None

    (length,) = struct.unpack("!I", header)
    if length == 0 or length > MAX_MESSAGE_SIZE:
        raise ProtocolError(f"Invalid message length: {length}")

    payload = recv_exact(sock, length)
    if payload is None:
        return None

    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"Invalid JSON message: {e}")

    if not isinstance(message, dict) or "type" not in message:
        raise ProtocolError("Message has no 'type' field")
    return message


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def make_hello(peer_id, peer_name, port):
    return {"type": HELLO, "peer_id": peer_id, "peer_name": peer_name, "port": port}


def make_hello_ack(peer_id, peer_name, port):
    return {"type": HELLO_ACK, "peer_id": peer_id, "peer_name": peer_name, "port": port}


def make_text(sender_id, sender_name, text):
    return {
        "type": TEXT,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message": text,
    }


def make_file(sender_id, sender_name, filename, filesize):
    return {
        "type": FILE,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "filename": filename,
        "filesize": filesize,
    }
