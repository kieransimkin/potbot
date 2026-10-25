"""HTML5 source patch is deliberately pinned and refuses unexpected upstream drift."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "frontend/scripts/prepare-xpra-html5.py"
spec = importlib.util.spec_from_file_location("potbot_xpra_vendor", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_headless_html_upstream_hooks_are_single_use() -> None:
    text = '''<html><head><script type="text/javascript" src="js/Client.js"></script></head>
<div id="screen" style="width: 100%; height: 100%"></div>
<script>
const path = getstrparam("path", ALLOWED_CHARS + "/") || window.location.pathname;
const client = new XpraClient("screen");
client.connect();
</script></html>'''
    result = module.patch_html(text)
    assert 'potbot-window-bridge.js' in result
    assert 'window.PotbotXpraBridge.bindClient(client)' in result
    assert 'client.reconnect = false' in result
    assert 'client.open_url = false' in result
    assert 'client.clipboard_enabled = false' in result
    assert 'client.on_connect = () => window.PotbotXpraBridge.ready()' in result
    assert 'left: -22000px' in result
    assert '.windowhead, .ui-resizable-handle' in result
    assert 'ALLOWED_CHARS + "/?=&"' in result
    with pytest.raises(ValueError, match="incompatible"):
        module.patch_html(result)
    with pytest.raises(ValueError, match="incompatible"):
        module.patch_html('<html></html>')


def test_pinned_upstream_source_hooks() -> None:
    """CI checks out the exact upstream revision and runs this real check."""
    import os
    import subprocess
    upstream_env = os.environ.get("POTBOT_XPRA_HTML5_SOURCE")
    if not upstream_env:
        pytest.skip("pinned Xpra HTML5 upstream checkout not available locally")
    upstream = Path(upstream_env)
    sha = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    assert sha == module.REVISION
    html5 = upstream / "html5"
    module.check_protocol_hooks(
        (html5 / "js" / "Client.js").read_text(encoding="utf-8"),
        (html5 / "js" / "Window.js").read_text(encoding="utf-8"),
    )
    result = module.patch_html((html5 / "index.html").read_text(encoding="utf-8"))
    assert "window.PotbotXpraBridge.bindClient(client);" in result
    assert "client.reconnect = false;" in result


def test_upstream_hook_drift_fails_closed() -> None:
    with pytest.raises(ValueError, match="Xpra client method unavailable"):
        module.check_protocol_hooks("class XpraClient {}", "class XpraWindow { draw() {} }")
