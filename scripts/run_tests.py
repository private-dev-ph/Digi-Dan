"""Run unittest tests with network access forbidden, including caught attempts."""
import argparse
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pattern",
        default="test_*.py",
        help="filename glob to focus discovery (default: test_*.py)",
    )
    args = parser.parse_args()
    if not args.pattern or Path(args.pattern).name != args.pattern or "/" in args.pattern or "\\" in args.pattern:
        parser.error("--pattern must be a filename glob without directory components")

    sys.path.insert(0, str(REPO))
    attempts = []
    internal = threading.local()
    original_connect = socket.socket.connect
    original_pair = socket.socketpair

    def socketpair(*call_args, **kwargs):
        # Python 3.10 on Windows implements asyncio's wakeup socketpair via an
        # internal loopback connection. Permit only that stdlib operation.
        internal.socketpair = True
        try:
            return original_pair(*call_args, **kwargs)
        finally:
            internal.socketpair = False

    def forbidden(*call_args, **kwargs):
        attempts.append(True)
        raise AssertionError("Network access forbidden in isolated tests")

    def guarded_connect(sock, address):
        if getattr(internal, "socketpair", False):
            return original_connect(sock, address)
        return forbidden(sock, address)

    # Keep discovery inside the guard because importing test modules can also
    # attempt network access.
    with patch.object(socket.socket, "connect", guarded_connect), \
            patch("socket.create_connection", forbidden), \
            patch("socket.socketpair", socketpair):
        suite = unittest.defaultTestLoader.discover(
            str(REPO / "scripts" / "tests"), pattern=args.pattern, top_level_dir=str(REPO)
        )
        if suite.countTestCases() == 0:
            print(f"No tests matched --pattern {args.pattern!r}", file=sys.stderr)
            return 2
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"Network attempts: {len(attempts)}")
    return 0 if result.wasSuccessful() and not attempts else 1


if __name__ == "__main__":
    raise SystemExit(main())
