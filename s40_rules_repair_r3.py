"""Strict, deterministic S40 source edits; no fuzzy substitution or git reset."""
from __future__ import annotations
import ast
import re


def replace_one(source: str, before: str, after: str, path: str) -> str:
    if source.count(after) == 1 and (source.count(before) == 0 or before in after):
        return source
    if source.count(before) != 1 or source.count(after) != 0:
        raise ValueError(f"S40 ambiguous/divergent source in {path}: {before[:80]!r}")
    return source.replace(before, after, 1)



def patch_app_view(source: str, path: str) -> str:
    """Update only `_App.view`'s AppView kind argument, preserving local formatting.

    S39-derived worktrees may wrap the AppView call or space its arguments
    differently. Exact text matching falsely rejects those variations. The
    AST validates the call's *meaning* and replaces only the second argument.
    An altered return contract is deliberately rejected, never overwritten.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("S40 app view source is invalid Python") from exc
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "_App"]
    if len(classes) != 1:
        raise ValueError(f"S40 requires one original _App class in {path}")
    methods = [node for node in classes[0].body
               if isinstance(node, ast.FunctionDef) and node.name == "view"]
    if len(methods) != 1:
        raise ValueError(f"S40 requires one original _App.view method in {path}")
    method = methods[0]
    returns = [node for node in method.body if isinstance(node, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.Call):
        raise ValueError("S40 _App.view return contract changed; inspect it before applying")
    call = returns[0].value
    if not isinstance(call.func, ast.Name) or call.func.id != "AppView":
        raise ValueError("S40 _App.view is no longer an AppView constructor")
    if len(call.args) == 7 and not call.keywords:
        actual = call.args[1]
    elif not call.args and len(call.keywords) == 7 and (
            {item.arg for item in call.keywords} ==
            {"id", "kind", "title", "state", "detached", "started_at", "exit_code"}):
        actual = next(item.value for item in call.keywords if item.arg == "kind")
    else:
        raise ValueError("S40 _App.view requires an understood seven-field AppView constructor")
    original = ast.parse("self.recipe.kind", mode="eval").body
    replacement = ast.parse('"agent-browser" if self.agent_id is not None else self.recipe.kind', mode="eval").body
    if ast.dump(actual, include_attributes=False) == ast.dump(replacement, include_attributes=False):
        return source
    if ast.dump(actual, include_attributes=False) != ast.dump(original, include_attributes=False):
        raise ValueError("S40 _App.view kind expression changed; refusing to overwrite it")
    # CPython AST offsets count UTF-8 bytes, rather than Python characters.
    # Replace byte ranges only within the verified expression; all other
    # whitespace, quotes, source comments and non-ASCII strings survive.
    lines = source.encode("utf-8").splitlines(keepends=True)
    start = sum(map(len, lines[:actual.lineno - 1])) + actual.col_offset
    end = sum(map(len, lines[:actual.end_lineno - 1])) + actual.end_col_offset
    before = source.encode("utf-8")
    changed = before[:start] + b'"agent-browser" if self.agent_id is not None else self.recipe.kind' + before[end:]
    result = changed.decode("utf-8")
    ast.parse(result)
    return result


def patch_start_admission(source: str, path: str) -> str:
    """Add the two agent guards to *the start method*, not an exact error-message block.

    S39 descendants can change the allowlist error message or add a comment
    between the kind validation and the lock without changing their semantics.
    Use Python's syntax tree to find the correct method/lock and keep those edits.
    Do not guess when the scope, validation expression or lock structure differs.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("S40 application supervisor source is invalid Python") from exc
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "XpraApplicationSupervisor"]
    if len(classes) != 1:
        raise ValueError("S40 missing unique application supervisor class")
    methods = [n for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "start"]
    if len(methods) != 1 or not isinstance(methods[0], ast.FunctionDef):
        raise ValueError("S40 missing unique application supervisor start method")
    method = methods[0]
    if [a.arg for a in method.args.kwonlyargs] != ["agent_id"]:
        raise ValueError("S40 agent start signature differs from its reviewed shape")
    old_kind_test = ast.parse("not isinstance(kind, str) or kind not in self.recipes", mode="eval").body
    kind_checks = [node for node in method.body if isinstance(node, ast.If)
                   and ast.dump(node.test, include_attributes=False) ==
                   ast.dump(old_kind_test, include_attributes=False)]
    if len(kind_checks) != 1 or not any(
        isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ScopeDeniedError"
        for node in kind_checks[0].body
    ):
        raise ValueError("S40 application-kind validation was replaced or removed")
    locked = [node for node in method.body if isinstance(node, ast.With)
              and len(node.items) == 1 and isinstance(node.items[0].context_expr, ast.Attribute)
              and node.items[0].context_expr.attr == "_lock"
              and isinstance(node.items[0].context_expr.value, ast.Name)
              and node.items[0].context_expr.value.id == "self"]
    if len(locked) != 1 or locked[0].lineno <= kind_checks[0].end_lineno:
        raise ValueError("S40 application launch lock does not match the reviewed shape")

    precheck = """        if agent_id is not None and (not isinstance(agent_id, UUID)
                                     or kind != "controlled-browser"
                                     or self.agent_displays is None):
            raise ScopeDeniedError("agent GUI launch requires a qualified private display pool")
"""
    duplicate = """            if agent_id is not None and any(
                app.agent_id == agent_id and app.process.poll() is None
                for app in self._apps.values()
            ):
                raise StateMismatchError("agent already has a live isolated Xpra browser")
"""
    # Reapplying a completed patch is harmless. An incomplete/foreign guard is not.
    region = source.splitlines(keepends=True)
    body = "".join(region[method.lineno - 1:method.end_lineno])
    markers = ("agent GUI launch requires a qualified private display pool",
               "agent already has a live isolated Xpra browser")
    if any(marker in body for marker in markers):
        if all(marker in body for marker in markers) and body.count(precheck) == 1 and body.count(duplicate) == 1:
            return source
        raise ValueError("S40 partially applied/changed agent admission guards; reconcile before continuing")

    # AST locates only this method; text insertion preserves all other source,
    # including a customised allowlist message and unrelated local modifications.
    lines = source.splitlines(keepends=True)
    with_line = locked[0].lineno - 1
    if lines[with_line].strip() != "with self._lock:":
        raise ValueError("S40 application lock uses unsupported multi-line syntax")
    lines.insert(with_line + 1, duplicate)
    lines.insert(with_line, precheck)
    result = "".join(lines)
    ast.parse(result)  # Never hand an unparseable result to the installer.
    return result


def transform(path: str, source: str) -> str:
    normal = source.replace('\r\n', '\n')
    if path == "src/potbot/tools/xpra_app_sessions.py":
        edits = [
            ('        self.browser_control: Any = None\n',
             '        self.browser_control: Any = None\n        self.agent_id: UUID | None = None\n'),
            ('                 browser_leases: Any = None,\n                 max_apps: int = 4,',
             '                 browser_leases: Any = None,\n                 agent_displays: Any = None,\n                 max_apps: int = 4,'),
            ('        self.browser_leases = browser_leases\n        self.max_apps, self.max_age_seconds =',
             '''        self.browser_leases = browser_leases
        if agent_displays is not None:
            from potbot.tools.agent_xpra_displays import AgentXpraDisplayPool
            if (not isinstance(agent_displays, AgentXpraDisplayPool)
                    or agent_displays.workspace_id != workspace_id
                    or agent_displays.principal_id != principal_id
                    or agent_displays.backend is not backend.backend
                    or browser_leases is None):
                raise ScopeDeniedError("agent Xpra pool differs from the reserved workspace")
        self.agent_displays = agent_displays
        self.max_apps, self.max_age_seconds ='''),
            ('        if proc.poll() is not None:\n            if app.browser_control is not None:\n                app.browser_control.close()\n            return\n',
             '''        if proc.poll() is not None:
            if app.browser_control is not None:
                app.browser_control.close()
            self._retire_agent_display(app)
            return
'''),
            ('        if app.browser_control is not None:\n            app.browser_control.close()\n\n    def _watch(self)',
             '''        if app.browser_control is not None:
            app.browser_control.close()
        self._retire_agent_display(app)

    def _retire_agent_display(self, app: _App) -> None:
        if self.agent_displays is None or app.agent_id is None:
            return
        if app.process.poll() is None:
            app.indeterminate = True
            return  # Never release a display while the process is still alive.
        try:
            self.agent_displays.retire(app.agent_id, process_stopped=True)
            if self.browser_leases is not None:
                self.browser_leases.retired(app.id, process_stopped=True)
        except (OSError, RuntimeError):
            app.indeterminate = True

    def _watch(self)'''),
            ("                expired = any(app.process.poll() is None and\n",
             """                for application in self._apps.values():
                    if application.agent_id is not None and application.process.poll() is not None:
                        self._retire_agent_display(application)
                expired = any(app.process.poll() is None and
"""),
            ('                    self.backend.server.environment()  # crash or lost X socket => fence GUI apps\n                    lost_display = False\n',
             '''                    self.backend.server.environment()  # shared display is separately checked
                    if self.agent_displays is not None:
                        for application in self._apps.values():
                            if application.agent_id is not None and application.process.poll() is None:
                                self.agent_displays.environment(application.agent_id)
                    lost_display = False
'''),
            ('    def start(self, kind: str) -> dict[str, Any]:\n',
             '    def start(self, kind: str, *, agent_id: UUID | None = None) -> dict[str, Any]:\n'),
            ('                launch = (self.backend.prepare(argv=self.recipes[kind].argv, status_fd=write_fd,\n',
             '''                launch = (self.agent_displays.prepare(agent_id, argv=self.recipes[kind].argv,
                                                     status_fd=write_fd)
                          if agent_id is not None else
                          self.backend.prepare(argv=self.recipes[kind].argv, status_fd=write_fd,
'''),
            ('                app = _App(uuid4().hex, self.recipes[kind], process, launch.identity)\n                if controlled_browser:\n',
             '''                app = _App(uuid4().hex, self.recipes[kind], process, launch.identity)
                app.agent_id = agent_id
                if controlled_browser:
'''),
            ('                    temporary = _App(uuid4().hex, self.recipes[kind], process, "")\n                    self._terminate(temporary)\n',
             '''                    temporary = _App(uuid4().hex, self.recipes[kind], process, "")
                    temporary.agent_id = agent_id
                    self._terminate(temporary)
'''),
            ('                self._settle()\n                raise\n            finally:\n                for fd in (write_fd, *(launch.pass_fds',
             '''                if process is None and agent_id is not None and self.agent_displays is not None:
                    # No subprocess was dispatched. The operator can safely
                    # retire a freshly started but unused private Xpra server.
                    self.agent_displays.retire(agent_id, process_stopped=True)
                self._settle()
                raise
            finally:
                for fd in (write_fd, *(launch.pass_fds'''),
            ('            if (app.recipe.kind != "controlled-browser" or app.process.poll() is not None\n                    or app.indeterminate):\n',
             '''            if (app.recipe.kind != "controlled-browser" or app.process.poll() is not None
                    or app.indeterminate or app.agent_id is not None):
'''),
            ('    def browser_command(self, identifier: str, command: object,\n                        *, lease_token: str | None = None) -> dict[str, Any]:\n',
             '''    def agent_browser_reservation(self, agent_id: UUID, identifier: str,
                                  action: str = "acquire", token: str | None = None) -> dict[str, Any]:
        """Trusted controller only; never exposed through the HTTP app routes."""
        with self._lock:
            app = self._get(identifier)
            if (self.browser_leases is None or app.agent_id != agent_id
                    or app.recipe.kind != "controlled-browser" or app.indeterminate
                    or app.process.poll() is not None or self._guard is None or self._guard.fenced):
                raise ScopeDeniedError("agent cannot reserve this isolated browser")
            if action == "acquire":
                lease = self.browser_leases.reserve(identifier, owner_kind="agent", owner_id=agent_id)
            elif action == "renew" and token is not None:
                lease = self.browser_leases.renew(identifier, token,
                            owner_kind="agent", owner_id=agent_id)
            elif action == "release" and token is not None:
                self.browser_leases.release(identifier, token, owner_kind="agent", owner_id=agent_id)
                return {"reservation": self.browser_leases.projection(identifier)}
            else:
                raise ScopeDeniedError("unapproved agent reservation action")
            return {"reservation": self.browser_leases.projection(identifier), "token": lease.token}

    def browser_command(self, identifier: str, command: object,
                        *, lease_token: str | None = None,
                        actor_agent_id: UUID | None = None) -> dict[str, Any]:
'''),
            ('            channel = app.browser_control\n        try:\n            if self.browser_leases is None:',
             '''            if app.agent_id != actor_agent_id and not (
                app.agent_id is None and self.agent_displays is None
            ):
                raise ScopeDeniedError("browser agent/owner identity differs from its display")
            channel = app.browser_control
        try:
            if self.browser_leases is None:'''),
            ('            with self.browser_leases.operation(identifier, lease_token):\n                return channel.call(command)\n',
             '''            with self.browser_leases.operation(identifier, lease_token,
                    owner_kind="agent" if actor_agent_id is not None else "operator",
                    owner_id=actor_agent_id or self.principal_id):
                return channel.call(command)
'''),
            ('        self._watcher.join(timeout=2)\n        if getattr(self.backend, "browser_gateway", None) is not None:\n',
             '''        self._watcher.join(timeout=2)
        if self.agent_displays is not None:
            if any(app.agent_id is not None and app.process.poll() is None for app in self._apps.values()):
                raise StateMismatchError("agent Xpra worker remains live during operator shutdown")
            self.agent_displays.stop_all()
        if getattr(self.backend, "browser_gateway", None) is not None:
'''),
            ('        egress_spec = data.get("browserEgress")\n',
             '''        display_spec = data.get("agentDisplays")
        if display_spec is not None:
            if (not isinstance(display_spec, dict)
                    or set(display_spec) != {"root", "workspaceName", "maxDisplays"}
                    or not isinstance(display_spec["root"], str)
                    or not Path(display_spec["root"]).is_absolute()
                    or not isinstance(display_spec["workspaceName"], str)
                    or not 1 <= len(display_spec["workspaceName"]) <= 80
                    or type(display_spec["maxDisplays"]) is not int
                    or not 1 <= display_spec["maxDisplays"] <= 12
                    or not strict or not any(r.kind == "controlled-browser" for r in recipes)):
                raise ValueError("agent display root requires reserved private Xpra browsers")
        egress_spec = data.get("browserEgress")
'''),
            ('        return XpraApplicationSupervisor(workspace_id=server.workspace_id, principal_id=principal,\n',
             '''        display_pool = None
        if display_spec is not None:
            from potbot.tools.agent_homes import AgentHomeRegistry
            from potbot.tools.agent_xpra_displays import AgentXpraDisplayPool
            display_pool = AgentXpraDisplayPool(
                workspace_id=server.workspace_id, principal_id=principal,
                operator_root=Path(display_spec["root"]), runtime=runtime,
                ledger=ledger, backend=backend,
                homes=AgentHomeRegistry(ledger, workspace_name=display_spec["workspaceName"]),
                browser_gateway=gateway, maximum=display_spec["maxDisplays"])
        return XpraApplicationSupervisor(workspace_id=server.workspace_id, principal_id=principal,
'''),
            ('                                         browser_leases=leases,\n                                         ai_monitor=monitor)\n',
             '''                                         browser_leases=leases,
                                         agent_displays=display_pool,
                                         ai_monitor=monitor)
'''),
        ]
        for before, after in edits:
            normal=replace_one(normal,before,after,path)
        normal = patch_app_view(normal, path)
        normal = patch_start_admission(normal, path)
        # Preserve separately installed S36 optional policy keys ("logs")
        # while rejecting duplicate/unknown optional-set source anchors.
        match = re.search(r'(?m)^        optional = \{([^\n]*)\}$', normal)
        if match is None or normal.count('        optional = {') != 1:
            raise ValueError("S40 missing the private operator-policy allowlist")
        fields = match.group(1)
        if '"browserLeases"' not in fields or '"strictXpraInput"' not in fields:
            raise ValueError("S40 operator policy has an unexpected source baseline")
        if '"agentDisplays"' not in fields:
            normal = normal[:match.end(1)] + ', "agentDisplays"' + normal[match.end(1):]
    elif path == "src/potbot/controller/browser_cogs.py":
        normal=replace_one(normal,
            '                raw = self.apps.browser_command(session, cmd, lease_token=reserved.token)\n',
            '                raw = self.apps.browser_command(session, cmd, lease_token=reserved.token,\n'
            '                                                actor_agent_id=reserved.owner_id)\n',path)
    elif path == "src/potbot/tools/browser_operator_policy.py":
        normal=replace_one(normal,
            '    if "browserEgress" in raw and (not strict or not isinstance(raw["browserEgress"], dict)):\n',
            '''    if "agentDisplays" in raw and (not strict or not isinstance(raw["agentDisplays"], dict)):
        raise ValueError("agent Xpra displays require reserved, readonly browser policy")
    if "browserEgress" in raw and (not strict or not isinstance(raw["browserEgress"], dict)):
''',path)
    return normal.replace('\n','\r\n') if '\r\n' in source else normal
