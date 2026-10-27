"""Single-process reference CAS store; operator integration is Stage 2."""
import hashlib
import os
from pathlib import Path
import re
import tempfile
from threading import RLock

ROLES = ('AGENTS.md', 'MODEL.md', 'MEMORY.md')
TYPES = ('sandbox', 'agent', 'operator', 'tool', 'skill', 'worker',
         'workflow', 'gateway', 'model', 'connector', 'primitive')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')

class Conflict(RuntimeError):
    pass

class InvalidContext(ValueError):
    pass

def validate(kind, name, role):
    if kind not in TYPES or not isinstance(name, str) or not NAME.fullmatch(name) or name == '..' or role not in ROLES:
        raise InvalidContext('invalid context identity or role')

def templates(kind, name):
    validate(kind, name, 'MODEL.md')
    title = f'{kind}:{name}'
    return {
        'AGENTS.md': f'# Instructions: {title}\n\nNo object-specific instructions configured.\n',
        'MODEL.md': f'# Working model: {title}\n\n## Observed state\n\nUnknown.\n\n## Uncertainties\n\nDiscovery pending.\n',
        'MEMORY.md': f'# History: {title}\n\nNo observations recorded.\n',
    }

def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()

class ContextStore:
    """Reference only: mutex/revisions are NOT durable or cross-process."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def path(self, kind, name, role):
        validate(kind, name, role)
        p = self.root / kind / name / role
        for segment in (p.parent.parent, p.parent, p):
            if segment.is_symlink():
                raise InvalidContext('symlink context path')
        return p

    def read(self, kind, name, role):
        with self.lock:
            p = self.path(kind, name, role)
            content = p.read_text(encoding='utf-8') if p.exists() else ''
            return content, digest(content)

    def write(self, kind, name, role, content, expected_digest):
        if not isinstance(content, str) or '\x00' in content or len(content.encode('utf-8')) > 1048576:
            raise InvalidContext('invalid document content')
        with self.lock:
            _, current = self.read(kind, name, role)
            if current != expected_digest:
                raise Conflict('stale revision')
            p = self.path(kind, name, role)
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='.context-')
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content.encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, p)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return digest(content)

    def provision(self, kind, name):
        for role, content in templates(kind, name).items():
            old, etag = self.read(kind, name, role)
            if not old:
                self.write(kind, name, role, content, etag)
