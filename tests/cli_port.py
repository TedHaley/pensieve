"""Tests of the port check that keeps a second `pensieve serve` from resetting the data under a running server
(pensieve/cli.py). Run: python tests/cli_port.py"""
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-cli-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import cli  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_taken():
    """A listening socket takes the port; a closed one leaves it free, also while it waits in TIME_WAIT."""
    port = _free_port()
    assert not cli._port_taken(port)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen()
    try:
        assert cli._port_taken(port)
        c = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.close()
        c.close()
    finally:
        srv.close()
    assert not cli._port_taken(port)


def test_serve_leaves_the_data_alone_when_the_port_is_taken():
    """`serve` stops before the version check: the data folder's version stamp and index stay as they are."""
    data = Path(os.environ["PENSIEVE_DATA"])
    (data / "v2").mkdir(parents=True, exist_ok=True)
    (data / "data-version").write_text("0.0.1")
    (data / "v2" / "index.db").write_text("an index of another version")
    port = _free_port()
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen()
    argv = sys.argv
    try:
        sys.argv = ["pensieve", "serve", "--no-open", "--port", str(port)]
        assert cli.main() == 1
    finally:
        sys.argv = argv
        srv.close()
    assert (data / "data-version").read_text() == "0.0.1"
    assert (data / "v2" / "index.db").exists()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
