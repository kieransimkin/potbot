"""Typed, public Agent security views with inherited Ed25519 implementation Embers.

Publishing these records does not grant signing authority. The local native
broker enforces its own encrypted policy, expected identity and custody checks.
Only the owner contract view contains the opaque signing-operation handle.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, SynType, parse_type_ref, type_identity
from potbot.security.public import (
    SIGNATURE_DOMAINS,
    public_identity_descriptor,
    verify_public_identity,
)

from .catalog import catalog_fingerprint, merge_types
from .embers import (
    EmberBinding,
    EmberContractBundle,
    EmberContractResolver,
    EmberContractScope,
    EmberInstance,
    EmberRevision,
    EmberSlot,
    TypeEmberContract,
)
from .hierarchy import profile_snapshot

SECURITY_PROFILE = "urn:potbot:agent-security:1"
_AUDIENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}\Z", re.ASCII)


def security_ref(slug: str) -> str:
    return f"synny://type/{slug}"


def property_ref(slug: str) -> str:
    return f"syn://thing/agent-security-{slug}"


# These are authored extensions. The imported native ontology is never rewritten.
_TYPE_SPECS = (
    (
        "wallet-agent",
        ("syn://type/agent",),
        "An autonomously acting agent or group of agents operating under one wallet key hierarchy.",
    ),
    (
        "wallet-agent-group",
        (security_ref("wallet-agent"), "syn://type/collective-agent"),
        "A group acting as one Agent under a single wallet key hierarchy.",
    ),
    (
        "agent-credential",
        ("syn://type/credential",),
        "The Agent authentication binding between its published identity and configured locking Cog.",
    ),
    (
        "configured-cog",
        ("syn://type/software-system",),
        "A configured persistent computational entity represented using the native Cog kind. "
        "Its invocation is a separate execution Event.",
    ),
    (
        "locking-cog",
        (security_ref("configured-cog"),),
        "A configured Cog enforcing rules for signing messages and verifying signatures.",
    ),
    (
        "ed25519-locking-cog",
        (security_ref("locking-cog"),),
        "A locking Cog using a stable Ed25519 base identity and base-certified hourly signing keys.",
    ),
    (
        "property-ember",
        ("syn://type/ember",),
        "A versioned knowledge Ember supplying one declared property value.",
    ),
    (
        "verification-key-ember",
        (security_ref("property-ember"),),
        "A public property Ember containing an Ed25519 verification key.",
    ),
    (
        "identity-hash-ember",
        (security_ref("property-ember"),),
        "A public property Ember containing SHA-256 of the raw base verification key.",
    ),
    (
        "credential-link-ember",
        (security_ref("property-ember"),),
        "A property Ember identifying the Agent Credential in the same selected snapshot.",
    ),
    (
        "locking-cog-link-ember",
        (security_ref("property-ember"),),
        "A property Ember identifying the configured locking Cog in the same selected snapshot.",
    ),
    (
        "agent-link-ember",
        (security_ref("property-ember"),),
        "A property Ember identifying the Agent governed by a Credential.",
    ),
    (
        "signing-policy-ember",
        (security_ref("property-ember"),),
        "A declared public signing policy; the native broker independently enforces its sealed policy.",
    ),
    (
        "code-implementation-ember",
        (security_ref("property-ember"),),
        "A code implementation descriptor containing source bytes and their SHA-256 digest.",
    ),
    (
        "private-signing-operation-ember",
        (security_ref("property-ember"),),
        "An owner-private opaque signing operation handle; never private key or recovery material.",
    ),
    (
        "public-identity-ember",
        (security_ref("property-ember"),),
        "A property Ember carrying the complete published base verification-key descriptor.",
    ),
    (
        "ed25519-public-key",
        ("syn://type/byte-sequence",),
        "A raw 32-byte Ed25519 public verification key, transported as canonical padded base64.",
    ),
    (
        "sha256-digest",
        ("syn://type/byte-sequence",),
        "A 32-byte SHA-256 digest, transported as 64 lowercase hexadecimal characters.",
    ),
    (
        "native-signing-operation",
        ("syn://type/information-object",),
        "An opaque local operation reference resolved only through the native custody boundary.",
    ),
    (
        "code-implementation-descriptor",
        ("syn://type/information-object",),
        "An exact source component and digest with a declared implementation role. "
        "A source digest establishes content identity, not security qualification.",
    ),
    (
        "signing-policy",
        ("syn://type/information-object",),
        "A workspace-scoped allowlist of audiences, semantic payload types and signing domains.",
    ),
    (
        "public-identity-descriptor",
        ("syn://type/information-object",),
        "A version-2 public base Ed25519 key and its stable SHA-256 Agent identity.",
    ),
    (
        "hourly-key-delegation",
        ("syn://type/information-object",),
        "A base-key-signed certificate binding an hourly verification key to its exact UTC epoch.",
    ),
    (
        "signature-envelope",
        ("syn://type/information-object",),
        "A version-2 typed, scoped, expiring signature envelope over an exact payload digest.",
    ),
    (
        "public-signature-bundle",
        ("syn://type/information-object",),
        "The public base identity, hourly delegation certificate and signature envelope.",
    ),
)


def security_types() -> tuple[SynType, ...]:
    result = tuple(
        SynType(
            parse_type_ref(security_ref(slug)),
            slug,
            tuple(parse_type_ref(parent) for parent in parents),
            definition,
            (),
            canonical_json(
                {
                    "authored_preferred_ref": security_ref(slug),
                    "profile": SECURITY_PROFILE,
                    "definition_version": 1,
                }
            ),
        )
        for slug, parents, definition in _TYPE_SPECS
    )
    inherited = profile_snapshot().types
    if {t.ref.slug for t in inherited} & {t.ref.slug for t in result}:
        raise InvalidInputError("security extension collides with the established hierarchy")
    return result


def security_type_snapshot() -> SynSnapshot:
    snapshot = SynSnapshot(
        "Potbot Agent security 1",
        "extend_builtin",
        (*profile_snapshot().types, *security_types()),
        (),
    )
    merge_types(snapshot)
    return snapshot


def validate_signing_policy(
    value: object, *, workspace_id: UUID, types: tuple[SynType, ...]
) -> dict[str, Any]:
    fields = {"workspace_id", "allowed_audiences", "allowed_payload_types", "allowed_domains"}
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidInputError("signing policy fields do not match the native policy schema")
    if value["workspace_id"] != str(workspace_id):
        raise InvalidInputError("signing policy must match the selected workspace")
    result: dict[str, Any] = {"workspace_id": str(workspace_id)}
    known = {t.ref.slug for t in types}
    for key, maximum in (
        ("allowed_audiences", 128),
        ("allowed_payload_types", 512),
        ("allowed_domains", 3),
    ):
        entries = value[key]
        if not isinstance(entries, list) or not 1 <= len(entries) <= maximum:
            raise InvalidInputError("signing policy requires bounded nonempty allowlists")
        if any(not isinstance(entry, str) for entry in entries):
            raise InvalidInputError("signing policy allowlists require strings")
        normalized = list(entries)
        if key == "allowed_audiences" and any(not _AUDIENCE.fullmatch(x) for x in normalized):
            raise InvalidInputError("invalid signing audience")
        if key == "allowed_domains" and not set(normalized) <= SIGNATURE_DOMAINS:
            raise InvalidInputError("unsupported signing domain")
        if key == "allowed_payload_types":
            identities = [type_identity(x) for x in normalized]
            if not set(identities) <= known:
                raise InvalidInputError("signing policy names a type outside the selected catalog")
            normalized = [f"syn://type/{identity}" for identity in identities]
        if len(set(normalized)) != len(normalized):
            raise InvalidInputError("duplicate signing policy value, including Type aliases")
        result[key] = sorted(normalized)
    return result


def _implementation_embers() -> tuple[EmberRevision, ...]:
    """Publish exact bundled source, never execute code obtained from an Ember."""
    from potbot.security.broker import native_source_directory

    security = files("potbot.security")
    native = native_source_directory()
    sources = [("public.py", security.joinpath("public.py"), "public-verification")]
    sources.extend(
        (f"native/{path.relative_to(native).as_posix()}", path, "native-signing-and-custody")
        for path in sorted(native.joinpath("src").rglob("*.rs"))
        if path.is_file()
    )
    sources.extend(
        (f"native/{name}", native.joinpath(name), "pinned-build-input")
        for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")
    )
    if not 5 <= len(sources) <= 32:
        raise InvalidInputError("the complete bundled native implementation is unavailable")
    result = []
    for name, path, role in sources:
        try:
            source = path.read_bytes()
            text = source.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise InvalidInputError("bundled implementation source is unavailable") from error
        digest = hashlib.sha256(source).hexdigest()
        result.append(
            EmberRevision.create(
                f"syn://ember/implementation-{digest}",
                security_ref("code-implementation-ember"),
                security_ref("code-implementation-descriptor"),
                {
                    "component": name,
                    "implementation_role": role,
                    "sha256": digest,
                    "source_utf8": text,
                    "profile": SECURITY_PROFILE,
                    "execution": "descriptive-only",
                    "qualification": "source-content-identity",
                },
            )
        )
    return tuple(result)


def _slot(
    slug: str,
    ember: str,
    value: str,
    *,
    private: bool = False,
    inherited: bool = False,
    maximum: int = 1,
) -> EmberSlot:
    return EmberSlot(
        property_ref(slug),
        slug,
        security_ref(ember),
        security_ref(value),
        1,
        maximum,
        "effective" if inherited else "instance",
        "private" if private else "public",
        "fixed",
    )


def _binding(slug: str, *values: EmberRevision) -> EmberBinding:
    return EmberBinding(property_ref(slug), tuple(v.pointer for v in values))


def _object(ref: str, type_ref: str, name: str, **payload: Any) -> SynObject:
    return SynObject(
        SynRef(ref),
        parse_type_ref(type_ref),
        canonical_json(
            {
                "id": ref,
                "typeRef": type_ref,
                "name": name,
                **payload,
            }
        ),
    )


@dataclass(frozen=True)
class AgentSecurityView:
    snapshot: SynSnapshot
    bundle: EmberContractBundle
    agent_ref: str
    credential_ref: str
    locking_cog_ref: str
    signer_hash: str

    def with_snapshot_id(self, snapshot_id: UUID) -> EmberContractBundle:
        scope = self.bundle.scope
        return EmberContractBundle(
            EmberContractScope(
                scope.workspace_id, scope.namespace, snapshot_id, scope.catalog_hash
            ),
            self.bundle.type_contracts,
            self.bundle.embers,
            self.bundle.instances,
            self.bundle.projection,
        )


def build_agent_security_view(
    identity: str | bytes | dict[str, object],
    *,
    expected_signer_hash: str,
    workspace_id: UUID,
    namespace: str,
    snapshot_id: UUID,
    policy: object,
    group: bool = False,
) -> AgentSecurityView:
    """Build typed records; owner-private handles never enter the public snapshot.

    The scope UUID is replaced with the actual sealed snapshot ID after import.
    Importing this view does not prove possession or change native signing policy.
    """
    verified = verify_public_identity(identity, expected_signer_hash=expected_signer_hash)
    descriptor = public_identity_descriptor(verified.public_key)
    base = security_type_snapshot()
    types = merge_types(base)
    checked_policy = validate_signing_policy(policy, workspace_id=workspace_id, types=types)
    digest = verified.signer_hash
    agent = f"syn://thing/agent-{digest}"
    credential = f"syn://credential/agent-{digest}"
    cog = f"syn://cog/ed25519-lock-{digest}"
    agent_type = security_ref("wallet-agent-group" if group else "wallet-agent")
    implementations = _implementation_embers()

    def ember(
        slug: str, etype: str, vtype: str, value: Any, *, kind: str = "json", private: bool = False
    ) -> EmberRevision:
        return EmberRevision.create(
            f"syn://ember/{slug}-{digest}",
            security_ref(etype),
            security_ref(vtype),
            value,
            value_kind=kind,
            visibility="private" if private else "public",
        )

    key = ember(
        "verification-key", "verification-key-ember", "ed25519-public-key", descriptor["public_key"]
    )
    fingerprint = ember("identity-hash", "identity-hash-ember", "sha256-digest", digest)
    public = ember(
        "public-identity", "public-identity-ember", "public-identity-descriptor", descriptor
    )
    credential_link = ember(
        "credential-link",
        "credential-link-ember",
        "agent-credential",
        credential,
        kind="object_ref",
    )
    cog_link = ember(
        "locking-cog-link", "locking-cog-link-ember", "ed25519-locking-cog", cog, kind="object_ref"
    )
    agent_link = ember(
        "agent-link",
        "agent-link-ember",
        "wallet-agent-group" if group else "wallet-agent",
        agent,
        kind="object_ref",
    )
    rules = ember("signing-policy", "signing-policy-ember", "signing-policy", checked_policy)
    handle = ember(
        "signing-operation",
        "private-signing-operation-ember",
        "native-signing-operation",
        f"secret://potbot/identity/{digest}/hourly-sign",
        kind="private_handle",
        private=True,
    )
    common_slots = (
        _slot("verification-key", "verification-key-ember", "ed25519-public-key"),
        _slot("identity-hash", "identity-hash-ember", "sha256-digest"),
        _slot("public-identity", "public-identity-ember", "public-identity-descriptor"),
    )
    common_bindings = (
        _binding("verification-key", key),
        _binding("identity-hash", fingerprint),
        _binding("public-identity", public),
    )
    contracts = (
        TypeEmberContract.create(
            security_ref("wallet-agent"),
            (*common_slots, _slot("credential", "credential-link-ember", "agent-credential")),
        ),
        TypeEmberContract.create(
            security_ref("agent-credential"),
            (
                *common_slots,
                _slot("agent", "agent-link-ember", "wallet-agent"),
                _slot("locking-cog", "locking-cog-link-ember", "ed25519-locking-cog"),
            ),
        ),
        TypeEmberContract.create(
            security_ref("locking-cog"),
            (
                *common_slots,
                _slot("credential", "credential-link-ember", "agent-credential"),
                _slot("signing-policy", "signing-policy-ember", "signing-policy"),
                _slot(
                    "signing-operation",
                    "private-signing-operation-ember",
                    "native-signing-operation",
                    private=True,
                ),
                _slot(
                    "implementation",
                    "code-implementation-ember",
                    "code-implementation-descriptor",
                    inherited=True,
                    maximum=32,
                ),
            ),
        ),
        TypeEmberContract.create(
            security_ref("ed25519-locking-cog"), (), (_binding("implementation", *implementations),)
        ),
    )
    instances = (
        EmberInstance.create(
            agent, agent_type, (*common_bindings, _binding("credential", credential_link))
        ),
        EmberInstance.create(
            credential,
            security_ref("agent-credential"),
            (*common_bindings, _binding("agent", agent_link), _binding("locking-cog", cog_link)),
        ),
        EmberInstance.create(
            cog,
            security_ref("ed25519-locking-cog"),
            (
                *common_bindings,
                _binding("credential", credential_link),
                _binding("signing-policy", rules),
                _binding("signing-operation", handle),
            ),
        ),
    )
    all_embers = (
        key,
        fingerprint,
        public,
        credential_link,
        cog_link,
        agent_link,
        rules,
        handle,
        *implementations,
    )
    scope = EmberContractScope(workspace_id, namespace, snapshot_id, catalog_fingerprint(types))
    bundle = EmberContractBundle(scope, contracts, all_embers, instances)
    EmberContractResolver(types, bundle, expected_scope=scope).validate_all()
    public_bundle = bundle.public_projection(types)
    objects = [
        _object(agent, agent_type, "Wallet Agent", credentialRef=credential),
        _object(
            credential,
            security_ref("agent-credential"),
            "Agent Credential",
            agentRef=agent,
            lockingCogRef=cog,
        ),
        _object(
            cog,
            security_ref("ed25519-locking-cog"),
            "Ed25519 locking Cog",
            credentialRef=credential,
        ),
    ]
    objects.extend(
        _object(e.ref, e.type_ref, e.ref.rsplit("/", 1)[-1], ember=e.to_dict())
        for e in public_bundle.embers
    )
    slots = {slot.slot_ref: slot for contract in contracts for slot in contract.slots}
    objects.extend(
        _object(ref, "syn://type/property", slot.name, securityProfile=SECURITY_PROFILE)
        for ref, slot in sorted(slots.items())
    )
    snapshot = SynSnapshot(base.source_label, base.catalog_mode, base.types, tuple(objects))
    return AgentSecurityView(snapshot, bundle, agent, credential, cog, digest)


def locking_cog_configuration(
    bundle: EmberContractBundle,
    types: tuple[SynType, ...],
    cog_ref: str,
    *,
    require_current_implementation: bool = False,
) -> dict[str, Any]:
    """Resolve an owner view, optionally enforcing this installation's source policy.

    Historical/imported source declarations remain usable for descriptive
    resolution. Executing the local signing path requires the complete inherited
    implementation revisions to equal the installed public verifier and selected
    native source/build inputs. This is a local content policy, not cryptographic
    proof that an executable was built from that source or security qualification.
    """
    if bundle.projection != "owner":
        raise InvalidInputError("signing requires the owner Ember view")
    resolved = (
        EmberContractResolver(types, bundle, expected_scope=bundle.scope)
        .resolve(cog_ref, projection="owner")
        .to_dict()
    )
    if type_identity(resolved["type_ref"]) != "ed25519-locking-cog":
        raise InvalidInputError("signing requires an exact supported Ed25519 locking Cog type")
    values = {slot["slot_ref"]: slot["values"] for slot in resolved["slots"]}
    if require_current_implementation:
        inherited = values.get(property_ref("implementation"), [])
        if not inherited or any(not item["source_types"] for item in inherited):
            raise InvalidInputError("local signing requires inherited code implementation Embers")
        expected = {(ember.ref, ember.revision) for ember in _implementation_embers()}
        actual = {(item["ember"]["ref"], item["ember"]["revision"]) for item in inherited}
        if actual != expected:
            raise InvalidInputError(
                "locking Cog implementation revisions do not match the current local implementation"
            )

    def one(name: str) -> Any:
        records = values.get(property_ref(name), [])
        if len(records) != 1:
            raise InvalidInputError("locking Cog requires exactly one checked property value")
        return records[0]["ember"]["value"]

    fingerprint = one("identity-hash")
    verified = verify_public_identity(one("public-identity"), expected_signer_hash=fingerprint)
    if one("verification-key") != public_identity_descriptor(verified.public_key)["public_key"]:
        raise InvalidInputError("locking Cog verification key differs from its public identity")
    if one("signing-operation") != f"secret://potbot/identity/{fingerprint}/hourly-sign":
        raise InvalidInputError("private operation handle differs from the pinned Agent identity")
    policy = validate_signing_policy(
        one("signing-policy"), workspace_id=bundle.scope.workspace_id, types=types
    )
    return {"signer_hash": fingerprint, "policy": policy}
