"""No test may use the network. Enforced, not assumed.

Imported first (discovery loads modules in name order before running any),
this makes every connection attempt raise NetworkUsedInTest. That is a
BaseException on purpose: the hunt treats ordinary errors from a source as
"source unavailable" and carries on, which is exactly how a test that tried
to reach live job sites went unnoticed -- it passed offline, and on the
server, with internet, it actually started scraping them.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import socket
import unittest


class NetworkUsedInTest(BaseException):
    pass


def _refuse(*args, **kwargs):
    raise NetworkUsedInTest(f"a test tried to open a network connection: {args[:2]!r}")


if not getattr(socket, "_jobscout_guard", False):
    socket.socket.connect = _refuse
    socket.socket.connect_ex = _refuse
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse
    socket._jobscout_guard = True


class Guard(unittest.TestCase):
    def test_connections_are_refused(self):
        with self.assertRaises(NetworkUsedInTest):
            socket.create_connection(("example.com", 443), timeout=1)
