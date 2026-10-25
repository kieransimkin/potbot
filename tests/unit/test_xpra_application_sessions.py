"""Application bridge tests never spawn a host process or require Xpra packages."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.tools.x11_workspace_server import WorkspaceXServer, create_app
from potbot.tools.xpra_app_sessions import AppRecipe, XpraApplicationSupervisor, _App
from potbot.domain.errors import InvalidInputError, ScopeDeniedError


class FakeApps:
    def __init__(self):
        self.ident = 'a' * 32
        self.running = True
        self.detached = False

    def choices(self):
        return [{'kind':'terminal','title':'Shell'}]

    def list_sessions(self):
        return [self.view()]

    def view(self):
        return {'id':self.ident,'kind':'terminal','title':'Shell','state':'running' if self.running else 'exited',
                'detached':self.detached,'startedAt':1700000000,'exitCode':None if self.running else 0}

    def start(self,kind):
        if kind!='terminal': raise ScopeDeniedError('not authorized')
        return self.view()

    def attach(self,identifier):
        self.check(identifier);self.detached=False;return self.view()

    def detach(self,identifier):
        self.check(identifier);self.detached=True;return self.view()

    def terminate(self,identifier):
        self.check(identifier);self.running=False;return self.view()

    def check(self,identifier):
        if identifier != self.ident: raise ScopeDeniedError('foreign session')


def test_app_bridge_auth_and_lifecycle(tmp_path):
    root = tmp_path / 'workspace';root.mkdir()
    server = WorkspaceXServer(uuid4(),root)
    apps = FakeApps()
    token = 'x'*48
    client = TestClient(create_app(server,token,'http://localhost:3100',apps))
    assert client.get('/v1/xpra/apps').status_code == 401
    headers = {'Authorization':'Bearer '+token}
    doc=client.get('/v1/xpra/apps',headers=headers).json()
    assert doc['workspaceId']==str(server.workspace_id)
    assert doc['sessions'][0]['id']==apps.ident
    assert client.post('/v1/xpra/apps',headers=headers,json={'kind':'terminal','argv':['/bin/sh']}).status_code==400
    assert client.post('/v1/xpra/apps',headers=headers,json={'kind':'../../invalid'}).status_code==409
    result = client.post('/v1/xpra/apps',headers=headers,json={'kind':'terminal'})
    assert result.status_code==201
    assert client.post('/v1/xpra/apps/bad/terminate',headers=headers).status_code==404
    assert client.post('/v1/xpra/apps/'+apps.ident+'/detach',headers=headers).json()['session']['detached']
    assert not client.post('/v1/xpra/apps/'+apps.ident+'/attach',headers=headers).json()['session']['detached']
    assert client.post('/v1/xpra/apps/'+apps.ident+'/terminate',headers=headers).json()['session']['state']=='exited'


def test_unconfigured_launcher_does_not_fall_back_to_host(tmp_path):
    root=tmp_path/'workspace';root.mkdir()
    server=WorkspaceXServer(uuid4(),root)
    client=TestClient(create_app(server,'x'*48,'http://localhost:3100'))
    headers={'Authorization':'Bearer '+'x'*48}
    assert client.get('/v1/xpra/apps',headers=headers).json()['configured'] is False
    assert client.post('/v1/xpra/apps',headers=headers,json={'kind':'terminal'}).status_code==503


def test_operator_recipe_is_exact_not_shell_or_arbitrary_argv():
    recipe=AppRecipe('terminal','xterm',('/usr/bin/xterm','-e','/bin/bash'))
    assert recipe.argv[0]=='/usr/bin/xterm'
    for argv in [('/bin/bash','-c','id'),('bash',),(),('/usr/bin/env','bash')]:
        with pytest.raises((InvalidInputError,ScopeDeniedError)):
            AppRecipe('terminal','Shell',argv)
    with pytest.raises(InvalidInputError): AppRecipe('../tool','Shell',('/usr/bin/xterm',))


def test_xpra_only_seccomp_profiles_cannot_be_combined():
    from potbot.tools.isolation import _seccomp_fd
    from potbot.domain.errors import CapabilityUnavailableError
    with pytest.raises(CapabilityUnavailableError):
        _seccomp_fd(private_x11_unix=True, private_loopback_ipc=True)
    try:
        fd=_seccomp_fd(private_x11_unix=True)
    except CapabilityUnavailableError:
        pytest.skip('libseccomp unavailable on this host')
    try:
        assert fd>=0
    finally:
        os.close(fd)



def test_xpra_ipc_seccomp_permits_unix_and_rejects_inet_in_a_child():
    """Execute the exported BPF: the AF_UNIX comparison must not be reversed."""
    import subprocess
    import sys
    from potbot.domain.errors import CapabilityUnavailableError
    from potbot.tools.isolation import _seccomp_fd
    try:
        fd = _seccomp_fd(private_x11_unix=True)
    except CapabilityUnavailableError:
        pytest.skip("host has no Linux libseccomp/BPF exporter")
    code = r'''import ctypes, errno, os, platform, socket, sys
fd=int(sys.argv[1]); data=os.read(fd, 262144)
assert data and len(data)%8 == 0
code=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
class FProg(ctypes.Structure):
    _fields_=[("length", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
prog=FProg(len(data)//8, ctypes.cast(code,ctypes.c_void_p).value)
libc=ctypes.CDLL(None,use_errno=True)
assert libc.prctl(38,1,0,0,0)==0, "no-new-privs unavailable"
number={"x86_64":317,"aarch64":277}.get(platform.machine())
if number is None: sys.exit(77)
rc=libc.syscall(ctypes.c_long(number),ctypes.c_long(1),ctypes.c_long(0),ctypes.byref(prog))
if rc != 0:
    sys.exit(77 if ctypes.get_errno() in (errno.EPERM,errno.ENOSYS) else 8)
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock: pass
try:
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM): pass
except OSError as e:
    assert e.errno==errno.EPERM, e
else:
    raise AssertionError("the GUI sandbox must not create IPv4 sockets")
try:
    a,b = socket.socketpair(); a.close(); b.close()
except OSError as e:
    raise AssertionError(f"AF_UNIX socketpair blocked: {e}")
'''
    try:
        result=subprocess.run([sys.executable,"-c",code,str(fd)], pass_fds=(fd,),
                              capture_output=True,text=True,timeout=12)
    finally:
        os.close(fd)
    if result.returncode == 77:
        pytest.skip("host forbids nested seccomp filter installation")
    assert result.returncode == 0, result.stderr


def test_grouped_application_lease_and_detach_do_not_kill_running_processes(monkeypatch):
    """Two UI applications share one admitted writer guard, not two conflicting leases."""
    import sys
    import types
    from potbot.tools.isolation import IsolationLaunch
    from potbot.tools import xpra_app_sessions as sessions
    workspace = uuid4()
    principal = uuid4()
    events=[]
    processes=[]

    class FakeGuard:
        fenced=False
        def start(self): events.append('lease-start')
        def finish(self,**kwargs): events.append('lease-finish')
        def abandon(self): events.append('lease-abandon')

    class GuardFactory:
        @staticmethod
        def exclusive_home(*args,**kwargs):return FakeGuard()

    fake_supervision = types.SimpleNamespace(
        WorkspaceLeaseGuard=GuardFactory,
        WorkspaceExecutorIdentity=lambda *args: args,
        admission_digest=lambda value: 'f'*64,
    )
    monkeypatch.setitem(sys.modules,'potbot.tools.workspace_supervision',fake_supervision)

    class FakeProcess:
        def __init__(self):self.pid=12345678;self.exit=None
        def poll(self):return self.exit

    class FakeBackend:
        server=types.SimpleNamespace(workspace_id=workspace, environment=lambda: {'DISPLAY':':412'})
        runtime=types.SimpleNamespace(image_digest='a'*64)
        def prepare(self,*,argv,status_fd):
            events.append(tuple(argv))
            return IsolationLaunch(('qualified-bwrap',*argv),(status_fd,),'hash-of-sandbox-admission')

    def fake_popen(*args,**kwargs):
        assert args[0][0]=='qualified-bwrap'
        assert kwargs['start_new_session'] is True
        process=FakeProcess();processes.append(process)
        return process

    service=XpraApplicationSupervisor(workspace_id=workspace,principal_id=principal,
        recipes=(AppRecipe('terminal','Terminal',('/usr/bin/xterm',)),
                 AppRecipe('browser','Browser',('/usr/bin/firefox','about:blank'))),
        backend=FakeBackend(),ledger=object(),popen=fake_popen)
    try:
        first=service.start('terminal');second=service.start('browser')
        assert events.count('lease-start')==1
        assert len(service.list_sessions())==2
        assert service.detach(first['id'])['detached'] is True
        assert len(processes)==2 and all(proc.poll() is None for proc in processes)
        assert service.attach(first['id'])['detached'] is False
        def stop_only(app): app.process.exit=0
        monkeypatch.setattr(service,'_terminate',stop_only)
        service.terminate(first['id'])
        assert events.count('lease-finish')==0
        service.terminate(second['id'])
        assert events.count('lease-finish')==1
        with pytest.raises(ScopeDeniedError):service.terminate('f'*32)
    finally:
        service.shutdown()


def test_xpra_bwrap_admission_mounts_only_its_workspaces_socket(tmp_path, monkeypatch):
    """Inspect the actual Bwrap argv, without launching an unconfined GUI process."""
    import socket
    import tempfile
    import shutil
    import sys
    import types
    from potbot.tools import xpra_app_sessions as sessions
    from potbot.tools.isolation import IsolationLaunch, _seccomp_fd
    from potbot.domain.errors import CapabilityUnavailableError
    workspace=uuid4()
    home=tmp_path/'workspace';(home/'.tmp').mkdir(parents=True)
    private=home/'.potbot'/'x11';private.mkdir(parents=True)
    authority=private/'Xauthority';authority.write_text('private cookie')
    authority.chmod(0o600)
    sock=socket.socket(socket.AF_UNIX)
    sockdir=Path(tempfile.mkdtemp(prefix='p-x11-',dir='/tmp'))
    socketpath=sockdir/'X412'
    sock.bind(str(socketpath));sock.listen()

    class FakeQualifiedBackend:
        runtime=types.SimpleNamespace(home=home)
        def prepare(self,**kwargs):
            fd=_seccomp_fd()
            status=kwargs['status_fd']
            return IsolationLaunch(('prlimit','--','bwrap','--unshare-all','--ro-bind',str(tmp_path),'/',
                                    '--proc','/proc','--clearenv','--chdir','/home',
                                    '--seccomp',str(fd),'--json-status-fd',str(status),'--',*kwargs['argv']),
                                   (fd,status),'base-isolation')
    server=types.SimpleNamespace(sandbox_root=home,workspace_id=workspace,
           socket_path=socketpath, auth_file=authority, environment=lambda:{'DISPLAY':':412'})
    fake_shared=types.SimpleNamespace(SharedWorkspaceBubblewrapBackend=FakeQualifiedBackend)
    fake_supervision=types.SimpleNamespace(validate_runtime_pin=lambda *args: None)
    monkeypatch.setitem(sys.modules,'potbot.tools.shared_workspace_isolation',fake_shared)
    monkeypatch.setitem(sys.modules,'potbot.tools.workspace_supervision',fake_supervision)
    fd=os.open(os.devnull,os.O_WRONLY)
    try:
        backend=sessions.XpraSandboxLaunchBackend(server=server,backend=FakeQualifiedBackend(),ledger=object())
        try:
            launch=backend.prepare(argv=('/usr/bin/xterm',),status_fd=fd)
        except CapabilityUnavailableError:
            pytest.skip('libseccomp missing for fake launch preflight')
        try:
            argv=list(launch.argv)
            index=argv.index(str(socketpath))
            assert argv[index-1]=='--ro-bind'
            assert argv[index+1]=='/home/.tmp/.X11-unix/X412'
            assert argv[argv.index('--setenv')+1]=='DISPLAY'
            assert 'XAUTHORITY' in argv
            assert argv.count('--unshare-all')==1
            assert argv.index('--seccomp')>argv.index('--chdir')
        finally:
            os.close(launch.pass_fds[0])
    finally:
        os.close(fd)
        sock.close()
        shutil.rmtree(sockdir)
