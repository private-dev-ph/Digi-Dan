"""Run Stage 5 tests with network access forbidden, including caught attempts."""
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
attempts = []
internal = threading.local()
original_connect = socket.socket.connect
original_pair = socket.socketpair

def socketpair(*args, **kwargs):
    internal.socketpair = True
    try:
        return original_pair(*args, **kwargs)
    finally:
        internal.socketpair = False

def forbidden(*args, **kwargs):
    attempts.append(True)
    raise AssertionError("Network access forbidden in Stage 5 isolated tests")

def guarded_connect(sock, address):
    if getattr(internal, "socketpair", False):
        return original_connect(sock, address)
    return forbidden(sock, address)

with patch.object(socket.socket, "connect", guarded_connect), patch("socket.create_connection", forbidden), patch("socket.socketpair", socketpair):
    suite = unittest.defaultTestLoader.discover(str(repo / "tests"), pattern="test_stage5*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
print(f"Network attempts: {len(attempts)}")
sys.exit(0 if result.wasSuccessful() and not attempts else 1)
