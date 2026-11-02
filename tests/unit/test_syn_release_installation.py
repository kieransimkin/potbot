"""SYN-R02 trust-boundary tests use disposable keys and synthetic package bytes."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import urllib.error
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, SourceUnavailableError
from potbot.syn_release.contracts import (
    Policy,
    canonical,
    document,
    inspect_wheel,
    verify_manifest,
)
from potbot.syn_release.source import GitHubSource, Publication, RemoteAsset


def make_bundle(v="0.1.0"):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    runtime = dict(
        profile="syn.runtime-contract/1",
        runtime_version=v,
        client_version=v,
        python_versions=["3.12", "3.13"],
        runtime_platforms=["linux-x86_64"],
        client_platforms=["linux-x86_64", "windows-x86_64"],
        node_modes=["service"],
        api_profiles=["syn.communication/1", "syn.consensus-policy/1"],
        capabilities=["http-workspace-messages"],
        database_revision="20261024_0015",
    )
    names = {
        "client-sdist": f"syn_client-{v}.tar.gz",
        "client-wheel": f"syn_client-{v}-py3-none-any.whl",
        "dependency-lock": f"syn-runtime-requirements-{v}.txt",
        "runtime-sdist": f"syn-{v}.tar.gz",
        "runtime-wheel": f"syn-{v}-py3-none-any.whl",
    }
    payloads = {name: ("synthetic " + role).encode() for role, name in names.items()}
    raw = canonical(
        dict(
            profile="syn.release-manifest/1",
            repository="Synsphere/syn",
            version=v,
            tag="v" + v,
            source_commit="1" * 40,
            runtime=runtime,
            artifacts=[
                dict(
                    role=role,
                    filename=name,
                    sha256=hashlib.sha256(payloads[name]).hexdigest(),
                    size=len(payloads[name]),
                )
                for role, name in sorted(names.items())
            ],
        )
    )
    fingerprint = hashlib.sha256(public).hexdigest()
    policy_value = dict(
        profile="potbot.syn-install-policy/1",
        repository="Synsphere/syn",
        version=v,
        source_commit="1" * 40,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        release_key_sha256=fingerprint,
        public_key=public.hex(),
        minimum_version=v,
        revoked_keys=[],
        revoked_manifests=[],
        component="client",
        mode=None,
        python_version="3.12",
        required_profiles=["syn.communication/1"],
        required_capabilities=[],
    )
    signature = canonical(
        dict(
            profile="syn.release-signature/1",
            manifest_sha256=policy_value["manifest_sha256"],
            release_key_sha256=fingerprint,
            signature=key.sign(b"syn.release-manifest/1\0" + raw).hex(),
        )
    )
    payloads[f"syn-release-{v}.json"] = raw
    payloads[f"syn-release-{v}.sig.json"] = signature
    return SimpleNamespace(
        raw=raw,
        signature=signature,
        value=policy_value,
        payloads=payloads,
        policy=Policy.parse(canonical(policy_value)),
        key=key,
    )


@pytest.fixture
def bundle():
    return make_bundle()


def test_authentication_and_compatibility_are_required_before_code(bundle):
    release = verify_manifest(bundle.raw, bundle.signature, bundle.policy)
    release.compatibility("linux-x86_64", "3.12")
    assert len(release.assets) == 5
    assert release.runtime["node_modes"] == ["service"]
    with pytest.raises(ScopeDeniedError):
        release.compatibility("linux-aarch64", "3.12")


@pytest.mark.parametrize(
    "change", ["manifest", "signature", "key", "source", "unknown", "duplicate", "pretty"]
)
def test_changed_or_ambiguous_trust_inputs_are_refused(bundle, change):
    raw, signature, value = bundle.raw, bundle.signature, deepcopy(bundle.value)
    if change == "manifest":
        raw += b" "
    elif change == "signature":
        envelope = json.loads(signature)
        envelope["signature"] = "0" * 128
        signature = canonical(envelope)
    elif change == "key":
        value["public_key"] = "0" * 64
    elif change == "source":
        value["source_commit"] = "2" * 40
    elif change == "unknown":
        value["download_url"] = "https://evil.invalid"
    elif change == "duplicate":
        raw = raw.replace(b'"tag":', b'"tag":"v0.1.0","tag":')
        value["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    elif change == "pretty":
        raw = json.dumps(json.loads(raw), indent=2).encode()
        value["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        verify_manifest(raw, signature, Policy.parse(canonical(value)))


@pytest.mark.parametrize(
    "name,value",
    [
        ("version", "latest"),
        ("minimum_version", "0.2.0"),
        ("revoked_keys", "key"),
        ("revoked_manifests", "manifest"),
        ("python_version", "3.14"),
    ],
)
def test_operator_version_revocation_and_python_policy_fail_closed(bundle, name, value):
    policy = deepcopy(bundle.value)
    if name == "revoked_keys":
        policy[name] = [policy["release_key_sha256"]]
    elif name == "revoked_manifests":
        policy[name] = [policy["manifest_sha256"]]
    else:
        policy[name] = value
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        Policy.parse(canonical(policy))


@pytest.mark.parametrize("mode", ["operator", "agent"])
def test_signed_service_release_cannot_admit_other_node_modes(bundle, mode):
    value = {**bundle.value, "component": "runtime", "mode": mode}
    release = verify_manifest(bundle.raw, bundle.signature, Policy.parse(canonical(value)))
    with pytest.raises(ScopeDeniedError, match="node mode"):
        release.compatibility("linux-x86_64", "3.12")


@pytest.mark.parametrize("member", ["../escape", "/absolute", "a\\escape", "C:/escape"])
def test_wheel_traversal_is_refused_without_unpacking(tmp_path, member):
    path = tmp_path / "bad.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        info = zipfile.ZipInfo("placeholder")
        info.filename = member
        wheel.writestr(info, "untrusted")
    with pytest.raises(ScopeDeniedError):
        inspect_wheel(path)
    assert not (tmp_path.parent / "escape").exists()


def test_wheel_symlink_is_refused(tmp_path):
    path = tmp_path / "bad.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        wheel.writestr(info, "/outside")
    with pytest.raises(ScopeDeniedError):
        inspect_wheel(path)


class Response(io.BytesIO):
    status = 200

    def __init__(self, raw):
        super().__init__(raw)
        self.headers = {"Content-Length": str(len(raw))}


def test_github_token_is_not_forwarded_to_asset_cdn():
    seen = []

    def open_request(req, **_kwargs):
        seen.append(req)
        if len(seen) == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                302,
                "redirect",
                {"Location": "https://release-assets.githubusercontent.com/official"},
                None,
            )
        return Response(b"asset")

    source = GitHubSource("private-test-token")
    source._opener = SimpleNamespace(open=open_request)
    output = io.BytesIO()
    assert source.download(RemoteAsset(1, "official", 5), output) == 5
    assert seen[0].get_header("Authorization") == "Bearer private-test-token"
    assert seen[1].get_header("Authorization") is None
    assert output.getvalue() == b"asset"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.invalid/asset",
        "http://release-assets.githubusercontent.com/a",
        "https://u:p@release-assets.githubusercontent.com/a",
    ],
)
def test_untrusted_redirects_never_receive_a_second_request(target):
    seen = []

    def open_request(req, **_kwargs):
        seen.append(req)
        raise urllib.error.HTTPError(req.full_url, 302, "redirect", {"Location": target}, None)

    source = GitHubSource("private-test-token")
    source._opener = SimpleNamespace(open=open_request)
    with pytest.raises(ScopeDeniedError):
        source.download(RemoteAsset(1, "official", 5), io.BytesIO())
    assert len(seen) == 1


def test_download_byte_bound_and_retry_budget():
    source = GitHubSource()
    source._opener = SimpleNamespace(open=lambda *_a, **_kw: Response(b"too much"))
    with pytest.raises(InvalidInputError):
        source.download(RemoteAsset(1, "official", 2), io.BytesIO())
    count = []

    def fail(*_args, **_kwargs):
        count.append(1)
        raise urllib.error.URLError("fixture transport failure")

    source._opener = SimpleNamespace(open=fail)
    with pytest.raises(SourceUnavailableError):
        source.download(RemoteAsset(1, "official", 2), io.BytesIO())
    assert len(count) == 2


@pytest.mark.parametrize("change", ["draft", "mutable", "prerelease", "duplicate", "tag", "source"])
def test_release_metadata_cannot_weaken_pins(bundle, monkeypatch, change):
    remote = dict(
        id=1,
        draft=False,
        prerelease=False,
        immutable=True,
        tag_name=bundle.policy.tag,
        html_url="https://github.com/Synsphere/syn/releases/tag/" + bundle.policy.tag,
        assets=[
            dict(id=i + 1, name=name, size=len(raw), state="uploaded")
            for i, (name, raw) in enumerate(bundle.payloads.items())
        ],
    )
    reference = {"object": {"type": "commit", "sha": bundle.value["source_commit"]}}
    if change in {"draft", "prerelease"}:
        remote[change] = True
    elif change == "mutable":
        remote["immutable"] = False
    elif change == "duplicate":
        remote["assets"][1]["name"] = remote["assets"][0]["name"]
    elif change == "tag":
        remote["tag_name"] = "v9.0.0"
    else:
        reference["object"]["sha"] = "2" * 40
    source = GitHubSource()
    monkeypatch.setattr(source, "_json", lambda suffix: reference if "/git/" in suffix else remote)
    with pytest.raises((ScopeDeniedError, InvalidInputError)):
        source.publication(bundle.policy)


class FakeSource(GitHubSource):
    """Synthetic bytes only; real official package acceptance runs in Syn CI."""

    def __init__(self, bundle):
        self.payloads = bundle.payloads
        self.calls = 0

    def publication(self, _policy):
        self.calls += 1
        return Publication(
            1,
            tuple(
                RemoteAsset(i + 1, name, len(raw))
                for i, (name, raw) in enumerate(self.payloads.items())
            ),
        )

    def download(self, asset, output):
        self.calls += 1
        return output.write(self.payloads[asset.name])


@pytest.mark.skipif(
    os.name != "posix", reason="Linux private-store ownership and flock qualification"
)
class TestLinuxStore:
    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        from potbot.syn_release.store import SynReleaseStore

        store = SynReleaseStore(tmp_path / "private-store")
        monkeypatch.setattr(store, "_tool", lambda p: (str(p), "2" * 64))

        def build(candidate, release, *_args):
            (candidate / "synthetic-package").write_bytes(b"unit fixture; not a Syn runtime")
            return {"client_version": release.policy.value["version"], "runtime": None}

        monkeypatch.setattr(store, "_build", build)
        monkeypatch.setattr(
            store,
            "_observe",
            lambda _candidate, release: {
                "client_version": release.policy.value["version"],
                "runtime": None,
            },
        )
        return store

    def test_offline_install_is_idempotent_and_records_exact_provenance(self, store, bundle):
        source = FakeSource(bundle)
        store.fetch(bundle.policy, source=source)
        calls = source.calls
        first = store.install(
            bundle.policy,
            python=Path("/fixture/python"),
            uv=Path("/fixture/uv"),
            source=source,
            offline=True,
        )
        second = store.install(
            bundle.policy,
            python=Path("/fixture/python"),
            uv=Path("/fixture/uv"),
            source=source,
            offline=True,
        )
        assert first["directory"] == second["directory"]
        assert source.calls == calls
        assert len(store._registry()) == 1
        assert first["manifest_sha256"] == bundle.value["manifest_sha256"]
        assert first["node_qualified"] is False
        assert first["acquisition"] == "offline_retained_signature_verified"
        assert store.verify(bundle.policy)["node_qualified"] is False

    def test_tampered_cache_never_reaches_install(self, store, bundle):
        from potbot.domain.errors import StateMismatchError

        release = store.fetch(bundle.policy, source=FakeSource(bundle))
        path = store.root / "objects" / release.assets[0].sha256
        path.chmod(0o600)
        path.write_bytes(b"altered")
        with pytest.raises(StateMismatchError):
            store.install(
                bundle.policy, python=Path("/fixture/python"), uv=Path("/fixture/uv"), offline=True
            )
        assert store._registry() == []

    def test_failed_build_and_incomplete_retry_are_not_admitted(self, store, bundle, monkeypatch):
        from potbot.domain.errors import StateMismatchError

        store.fetch(bundle.policy, source=FakeSource(bundle))

        def fail(*_args):
            assert store._registry() == []
            raise StateMismatchError("synthetic interrupted build")

        monkeypatch.setattr(store, "_build", fail)
        for _ in range(2):
            with pytest.raises(StateMismatchError):
                store.install(
                    bundle.policy,
                    python=Path("/fixture/python"),
                    uv=Path("/fixture/uv"),
                    offline=True,
                )
        assert store._registry() == []

    def test_retained_history_prevents_downgrade(self, store, bundle):
        higher = make_bundle("0.2.0")
        store.install(
            higher.policy,
            python=Path("/fixture/python"),
            uv=Path("/fixture/uv"),
            source=FakeSource(higher),
        )
        with pytest.raises(ScopeDeniedError, match="downgrade"):
            store.install(
                bundle.policy,
                python=Path("/fixture/python"),
                uv=Path("/fixture/uv"),
                source=FakeSource(bundle),
            )
        assert len(store._registry()) == 1

    def test_changed_installed_file_and_changed_permissions_are_refused(self, store, bundle):
        from potbot.domain.errors import StateMismatchError

        record = store.install(
            bundle.policy,
            python=Path("/fixture/python"),
            uv=Path("/fixture/uv"),
            source=FakeSource(bundle),
        )
        candidate = store.root / "installs" / record["directory"]
        candidate.chmod(0o700)
        with pytest.raises(StateMismatchError):
            store.verify(bundle.policy)
        candidate.chmod(0o500)
        artifact = candidate / "synthetic-package"
        artifact.chmod(0o600)
        artifact.write_bytes(b"tampered")
        artifact.chmod(0o400)
        with pytest.raises(StateMismatchError):
            store.verify(bundle.policy)

    def test_journal_path_escape_is_refused(self, store, bundle):
        store.install(
            bundle.policy,
            python=Path("/fixture/python"),
            uv=Path("/fixture/uv"),
            source=FakeSource(bundle),
        )
        journal = store.root / "registry.json"
        value = json.loads(journal.read_bytes())
        value["installations"][0]["files_sha256"] = "/etc/passwd"
        journal.write_bytes(canonical(value))
        with pytest.raises(InvalidInputError):
            store.verify(bundle.policy)

    def test_revocation_is_rechecked_for_existing_install(self, store, bundle):
        store.install(
            bundle.policy,
            python=Path("/fixture/python"),
            uv=Path("/fixture/uv"),
            source=FakeSource(bundle),
        )
        bundle.policy.value["revoked_keys"] = [bundle.value["release_key_sha256"]]
        with pytest.raises(ScopeDeniedError, match="revoked"):
            store.verify(bundle.policy)

    def test_dependency_cache_tamper_is_refused_against_retained_selection(self, store, bundle):
        from potbot.domain.errors import StateMismatchError

        item = store.root / "uv-cache" / "synthetic-dependency"
        item.write_bytes(b"prepared dependency fixture")
        selected_digest = store._seal_dependency_cache()
        selected = Policy.parse(
            canonical(
                {
                    **bundle.value,
                    "component": "runtime",
                    "mode": "service",
                    "dependency_cache_sha256": selected_digest,
                }
            )
        )
        store._check_dependency_cache(selected, [], True)
        item.write_bytes(b"altered dependency fixture")
        with pytest.raises(StateMismatchError, match="dependency cache"):
            store._check_dependency_cache(selected, [], True)

    def test_fresh_offline_runtime_requires_independent_cache_selection(self, store, bundle):
        selected = Policy.parse(
            canonical({**bundle.value, "component": "runtime", "mode": "service"})
        )
        with pytest.raises(ScopeDeniedError, match="dependency-cache digest"):
            store._check_dependency_cache(selected, [], True)

    def test_changed_artifact_provenance_is_refused(self, store, bundle):
        from potbot.domain.errors import StateMismatchError

        store.install(
            bundle.policy,
            python=Path("/fixture/python"),
            uv=Path("/fixture/uv"),
            source=FakeSource(bundle),
        )
        journal = store.root / "registry.json"
        value = json.loads(journal.read_bytes())
        value["installations"][0]["artifacts"][0]["sha256"] = "0" * 64
        journal.write_bytes(canonical(value))
        with pytest.raises(StateMismatchError, match="provenance"):
            store.verify(bundle.policy)

    def test_concurrent_install_has_one_admission(self, store, bundle):
        from concurrent.futures import ThreadPoolExecutor

        store.fetch(bundle.policy, source=FakeSource(bundle))

        def install():
            return store.install(
                bundle.policy, python=Path("/fixture/python"), uv=Path("/fixture/uv"), offline=True
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: install(), range(2)))
        assert results[0]["directory"] == results[1]["directory"]
        assert len(store._registry()) == 1


def test_cli_help_is_portable_and_imports_no_node(capsys):
    from potbot.cli import main

    with pytest.raises(SystemExit) as result:
        main(["syn-release", "--help"])
    assert result.value.code == 0
    assert "independently pinned official Syn" in capsys.readouterr().out


def test_ambiguous_json_is_rejected_before_trust_selection():
    with pytest.raises(InvalidInputError):
        document(b'{"version":"0.1.0","version":"9.0.0"}')


@pytest.mark.parametrize(
    "text",
    [
        "--extra-index-url https://evil.invalid",
        "-r /private/file",
        "package @ https://evil.invalid/file",
        "package>=1",
        "--trusted-host example.invalid",
    ],
)
def test_dependency_lock_cannot_override_download_or_source_policy(tmp_path, text):
    from potbot.syn_release.contracts import inspect_requirements

    lock = tmp_path / "requirements.txt"
    lock.write_text(text)
    with pytest.raises(ScopeDeniedError):
        inspect_requirements(lock)


def test_signed_uv_export_form_is_accepted(tmp_path):
    from potbot.syn_release.contracts import inspect_requirements

    lock = tmp_path / "requirements.txt"
    lock.write_text(
        "# exported\nexample[binary]==1.2.3 ; python_version >= '3.12' \\\n    --hash=sha256:"
        + "a" * 64
        + "\n"
    )
    inspect_requirements(lock)
