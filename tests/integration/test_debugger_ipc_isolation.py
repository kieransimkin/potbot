"""Private debugger IPC is qualified independently from the default deny profile."""

import socket
import sys

import pytest

from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor

pytestmark = pytest.mark.sandbox


def test_private_tcp_ipc_cannot_reach_host_network_or_path_sockets(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "private-canary"
    canary.write_text("private-fixture-value")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "escape").symlink_to(canary)
    with socket.socket() as host:
        host.bind(("127.0.0.1", 0))
        host.listen()
        host.settimeout(0.2)
        port = host.getsockname()[1]
        program = rf"""
import socket,threading,subprocess,sys
for path in [{str(canary)!r},'escape']:
    try:open(path).read()
    except OSError:pass
    else:raise AssertionError('filesystem escape')
for family,kind,proto in [(socket.AF_UNIX,socket.SOCK_STREAM,0),
                          (socket.AF_INET,socket.SOCK_DGRAM,0),
                          (socket.AF_INET,socket.SOCK_RAW,0),
                          (socket.AF_PACKET,socket.SOCK_RAW,0)]:
    try:socket.socket(family,kind,proto)
    except OSError:pass
    else:raise AssertionError('non-TCP socket permitted')
for endpoint in [('127.0.0.1',{port}),('1.1.1.1',443)]:
    with socket.socket() as client:
        client.settimeout(0.3)
        try:client.connect(endpoint)
        except OSError:pass
        else:raise AssertionError('host/external connection escaped')
left,right=socket.socketpair()
left.sendall(b'wakeup');assert right.recv(6)==b'wakeup'
left.close();right.close()
server=socket.socket();server.bind(('127.0.0.1',0));server.listen()
def serve():
    conn,_=server.accept()
    with conn:assert conn.recv(4)==b'ping';conn.sendall(b'pong')
thread=threading.Thread(target=serve);thread.start()
with socket.socket() as client:
    client.settimeout(2);client.connect(server.getsockname());client.sendall(b'ping');assert client.recv(4)==b'pong'
thread.join(2);assert not thread.is_alive();server.close()
child='import socket\ntry:socket.socket(socket.AF_UNIX)\nexcept PermissionError:pass\nelse:raise AssertionError("child policy escape")'
assert subprocess.run([sys.executable,'-I','-c',child]).returncode==0
print('private loopback IPC verified')
"""
        result = ProcessSupervisor().run(
            ProcessRequest(
                (sys.executable, "-I", "-c", program),
                candidate,
                candidate,
                sandbox_root=tmp_path,
                ipc_profile="private-loopback-v1",
                timeout_ms=10_000,
            )
        )
        assert result.outcome == "completed", result
        assert result.stdout == b"private loopback IPC verified\n"
        assert result.processes_stopped and len(result.isolation_identity) == 64
        with pytest.raises(TimeoutError):
            host.accept()
    assert canary.read_text() == "private-fixture-value"
