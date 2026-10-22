"""External policy inputs, cost ceilings and incremental credential redaction."""

from __future__ import annotations

import base64
from urllib.parse import quote

import pytest

from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    ScopeDeniedError,
)
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.models.providers import ProviderRequest, ProviderSelection, ProviderUsage
from potbot.models.stream_redaction import StreamingRedactor
from potbot.service_credentials import PROVIDER_ENVIRONMENT, ServiceCredentials
from potbot.tools.secrets import CaptureRedactor


def rule_value(**changes):
    return {
        "provider": "openai",
        "model": "test-model",
        "input_microunits_per_million": 2_000_000,
        "output_microunits_per_million": 8_000_000,
        **changes,
    }


def policy_value(**changes):
    return {
        "schema_version": 1,
        "allow_prompt_disclosure": True,
        "rules": [rule_value()],
        "max_prompt_bytes": 10000,
        "max_output_tokens": 8192,
        "max_timeout_seconds": 300,
        "max_cost_microunits": 1_000_000,
        **changes,
    }


def selected(**changes):
    return {"provider": "openai", "model": "test-model", "role": "reasoning", **changes}


@pytest.mark.parametrize(
    "roles",
    [
        [[]],
        [{}],
        [None],
        [1],
        [True],
        ["reasoning", []],
        ["reasoning", "reasoning"],
        "reasoning",
        None,
    ],
)
def test_bad_rule_roles_have_classified_input_errors(roles):
    with pytest.raises(DomainError):
        RateRule.from_dict(rule_value(roles=roles))


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", []),
        ("provider", None),
        ("model", []),
        ("model", {}),
        ("protocol", []),
        ("protocol", "messages"),
        ("input_microunits_per_million", True),
        ("input_microunits_per_million", -1),
        ("output_microunits_per_million", 1.5),
    ],
)
def test_bad_rule_configuration_is_classified(field, value):
    with pytest.raises(DomainError):
        RateRule.from_dict(rule_value(**{field: value}))


@pytest.mark.parametrize(
    "value",
    [None, [], ["prompt"], "prompt", 5, {"prompt": []}, {"prompt": "text", "api_key": "secret"}],
)
def test_request_input_requires_a_typed_bounded_mapping(value):
    with pytest.raises(DomainError):
        HostedPolicy.from_dict(policy_value()).request(selected(), value)


@pytest.mark.parametrize(
    "selection",
    [
        None,
        [],
        {},
        {"provider": "openai"},
        selected(role=[]),
        selected(provider=[]),
        selected(model={}),
        selected(unapproved="field"),
    ],
)
def test_request_selection_requires_only_named_provider_model_role(selection):
    with pytest.raises(DomainError):
        HostedPolicy.from_dict(policy_value()).request(selection, {"prompt": "test"})


def test_policy_default_denies_disclosure_and_rules_are_required():
    request = ProviderRequest(ProviderSelection("openai", "test-model"), "test")
    with pytest.raises(ScopeDeniedError):
        HostedPolicy().authorize(request)
    with pytest.raises(ScopeDeniedError):
        HostedPolicy.from_dict(policy_value(rules=[])).authorize(request)


def test_exact_rule_and_role_override_wildcard():
    rules = [
        rule_value(model="*", roles=["reasoning", "coding"]),
        rule_value(roles=["coding"], input_microunits_per_million=3_000_000),
    ]
    policy = HostedPolicy.from_dict(policy_value(rules=rules))
    with pytest.raises(ScopeDeniedError):
        policy.request(selected(), {"prompt": "test"})
    _, _, rule = policy.request(selected(role="coding"), {"prompt": "test"})
    assert rule.input_microunits_per_million == 3_000_000
    assert policy.rule("openai", "future-model", "reasoning").model == "*"


def test_authorizer_enforces_configured_protocol():
    policy = HostedPolicy.from_dict(policy_value(rules=[rule_value(protocol="responses")]))
    with pytest.raises(ScopeDeniedError):
        policy.authorize(
            ProviderRequest(ProviderSelection("openai", "test-model", "chat_completions"), "test")
        )


def test_token_admission_uses_utf8_bytes_and_preserves_larger_user_allowance():
    policy = HostedPolicy.from_dict(policy_value())
    request, allowance, _ = policy.request(selected(), {"prompt": "é🪴", "system": "instructions"})
    assert allowance == len((request.prompt + request.system).encode()) + 1024
    with pytest.raises(InvalidInputError):
        policy.request(selected(), {"prompt": "test", "input_token_allowance": 4})
    _, larger, _ = policy.request(selected(), {"prompt": "test", "input_token_allowance": 10000})
    assert larger == 10000


@pytest.mark.parametrize(
    "config,input_value",
    [
        ({"max_prompt_bytes": 3}, {"prompt": "éé"}),
        ({"max_output_tokens": 10}, {"prompt": "test", "max_output_tokens": 11}),
        ({"max_timeout_seconds": 5}, {"prompt": "test", "timeout_seconds": 6}),
        ({"max_cost_microunits": 1}, {"prompt": "test"}),
    ],
)
def test_disclosure_and_spend_bounds_reject_before_dispatch(config, input_value):
    with pytest.raises(ScopeDeniedError):
        HostedPolicy.from_dict(policy_value(**config)).request(selected(), input_value)


def test_cost_rounding_and_unclassified_reported_tokens_use_safe_upper_rate():
    cheap = RateRule.from_dict(
        rule_value(input_microunits_per_million=1, output_microunits_per_million=1)
    )
    assert cheap.charge(1, 0) == 1
    rule = RateRule.from_dict(rule_value())
    assert rule.charge(10, 20, total_tokens=35) == 220
    amounts = HostedPolicy.usage_amounts(ProviderUsage(10, 20, 35, 3, 0, 9), rule, 17)
    assert amounts == {"calls": 1, "tokens": 35, "inference_ms": 17, "money_microunits": 220}


def test_policy_rejects_duplicated_rules_and_external_secret_fields():
    for config in (
        policy_value(rules=[rule_value(), rule_value()]),
        policy_value(api_key="secret"),
    ):
        with pytest.raises(InvalidInputError):
            HostedPolicy.from_dict(config)


class FakeStore:
    def __init__(self, values=None, error=None):
        self.values, self.error, self.reads = values or {}, error, []

    def get(self, name):
        self.reads.append(name)
        if self.error:
            raise self.error
        return self.values.get(name)


@pytest.mark.parametrize("provider,variable", list(PROVIDER_ENVIRONMENT.items()))
def test_provider_credential_references_use_exact_env_names_then_native_store(provider, variable):
    store = FakeStore({f"providers.{provider}": "stored-secret"})
    creds = ServiceCredentials(environment={variable: "environment-secret"}, store=store)
    assert creds.resolve(f"secret://providers/{provider}") == "environment-secret"
    assert store.reads == []
    creds = ServiceCredentials(environment={}, store=store)
    assert creds.resolve(f"secret://providers/{provider}") == "stored-secret"
    assert store.reads == [f"providers.{provider}"]


@pytest.mark.parametrize(
    "reference",
    [
        None,
        1,
        [],
        {},
        "secret://providers/unknown",
        "secret://providers/openai/extra",
        "secret://api/unknown",
        "https://bad.invalid",
    ],
)
def test_bad_service_references_are_classified_without_reading_store(reference):
    store = FakeStore()
    with pytest.raises(DomainError):
        ServiceCredentials(environment={}, store=store).resolve(reference)
    assert store.reads == []


@pytest.mark.parametrize(
    "value", ["", "short", "x" * 31, "x" * 4097, "x" * 32 + "\n", "é" * 32, "x" * 32 + "\x85"]
)
def test_api_credentials_require_bounded_visible_ascii_tokens(value):
    with pytest.raises(CapabilityUnavailableError):
        ServiceCredentials(environment={"POTBOT_API_TOKEN": value}, store=FakeStore()).resolve(
            "secret://api/access-token"
        )


def test_api_credential_native_fallback_and_locked_store_error_are_sanitized():
    token = "token-" + "a" * 32
    assert (
        ServiceCredentials(environment={}, store=FakeStore({"api.access_token": token})).resolve(
            "secret://api/access-token"
        )
        == token
    )
    creds = ServiceCredentials(
        environment={}, store=FakeStore(error=CapabilityUnavailableError("private-backend-canary"))
    )
    with pytest.raises(CapabilityUnavailableError) as caught:
        creds.resolve("secret://providers/evolink")
    assert "private-backend-canary" not in str(caught.value)
    assert creds.configured("evolink") is False


def test_stream_redactor_preserves_utf8_and_redacts_every_secret_split():
    secret = "clé-🔑-秘密"
    capture = CaptureRedactor((secret, "short-secret"))
    forms = [secret, quote(secret, safe=""), base64.b64encode(secret.encode()).decode()]
    for form in forms:
        text = "préfixe 🪴" * 10 + form + " end 🦇" * 10
        expected = capture.redact_bytes(text.encode()).decode()
        for cut in range(len(text) + 1):
            redactor = StreamingRedactor(capture)
            result = (
                redactor.feed(text[:cut])
                + redactor.feed(text[cut:])
                + redactor.feed("", final=True)
            )
            assert result == expected
            assert secret not in result and form not in result


def test_interrupted_stream_redacts_partial_secret_suffix():
    secret = "private-provider-key"
    capture = CaptureRedactor((secret,))
    for cut in range(1, len(secret)):
        redactor = StreamingRedactor(capture)
        text = "some public result " * 20 + secret[:cut]
        result = redactor.feed(text) + redactor.feed("", final=True, complete=False)
        assert result == capture.redact_bytes(text.encode(), complete=False).decode()


def test_stream_redactor_rejects_whole_output_rejection_rules():
    with pytest.raises(InvalidInputError):
        StreamingRedactor(CaptureRedactor(reject_patterns=(b"forbidden-output",)))
