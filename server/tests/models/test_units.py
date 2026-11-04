"""Pure units of the model-call layer (D15): config, egress, back-off, audio, run tokens, helpers.

No test here calls a network host: every provider is an httpx.MockTransport, and every wait goes
to an injected sleep that records it. Only the run-token tests touch the store, for a world id.
"""

import asyncio
import base64
import copy
import hashlib
import hmac
import io
import json
import math
import wave
from datetime import UTC, date, datetime

import httpx
import pytest

from grove import clock
from grove.clock import TestClock
from grove.models import config
from grove.models.audio import API_LIMIT_SECONDS, PIECE_SECONDS, split_wav, wav_seconds
from grove.models.config import (
    DEFAULT_DAILY_BUDGET,
    DEFAULT_GATEWAY_HOST,
    DEFAULT_GATEWAY_PORT,
    DEFAULT_META_BASE_URL,
    Catalog,
    Job,
    ModelSettings,
    ModelSpec,
    Price,
    load_catalog,
    load_model_settings,
)
from grove.models.egress import (
    AGENTS,
    CLEANUP_AGENT,
    MODEL_LAYER,
    RUNTIMES,
    AllowlistTransport,
    AsyncAllowlistTransport,
    EgressPolicy,
)
from grove.models.errors import (
    AudioTooLong,
    BudgetReached,
    EgressRefused,
    InvalidOutput,
    ModelRefused,
    NotConfigured,
    Unauthenticated,
)
from grove.models.gateway_auth import MAX_TTL_SECONDS, RunClaims, mint, verify
from grove.models.http import MAX_ATTEMPTS, MAX_WAIT, MODAL_ATTEMPTS, asend, reset_after, send
from grove.models.layer import (
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_PROPERTIES,
    Usage,
    chat_usage,
    check_schema,
    default_cache_key,
    glimmer_effort_fields,
    parse_structured,
    responses_text,
    responses_usage,
)
from grove.models.meter import reached, resets_at
from grove.store.errors import ScopeError
from grove.writer.model_calls import BudgetState

SPARK = "muse-spark-1.3"
VOICE = "muse-voice-transcribe-1.0"
GLIMMER = "muse-glimmer-30b"
META_HOST = "api.meta.ai"
GLIMMER_HOST = "glimmer.example.test"
MODAL_HOST = "ws--grove-glimmer-glimmerserver.eu-west.modal.direct"
SECRET = b"s" * 32
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SETTINGS = ModelSettings(
    meta_key="test-meta-key",
    glimmer_base_url="https://glimmer.example.test/v1",
    glimmer_key="test-glimmer-key",
    daily_budget=5.0,
    gateway_secret=SECRET,
)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog()


# (1) config: the catalog the package ships.


def test_catalog_families_point_at_the_three_shipped_models(catalog):
    assert catalog.families == {"spark": SPARK, "voice": VOICE, "glimmer": GLIMMER}
    assert catalog.models == {
        SPARK: ModelSpec(SPARK, "spark", "meta", ("minimal", "low", "medium", "high", "max"), True),
        VOICE: ModelSpec(VOICE, "voice", "meta", (), False),
        GLIMMER: ModelSpec(GLIMMER, "glimmer", "modal", ("low", "medium", "high"), True),
    }


def test_job_table_marks_which_jobs_continue_and_which_defer_at_the_ceiling(catalog):
    assert {name: job.at_ceiling for name, job in catalog.jobs.items()} == {
        "triage": "continue",
        "triage_fallback": "continue",
        "extraction": "continue",
        "transcription": "continue",
        "image_read": "continue",
        "backfill_triage": "defer",
        "backfill_extraction": "defer",
        "update_pass": "defer",
        "verification": "defer",
        "second_opinion": "defer",
        "word_judge": "defer",
        "second_opinion_fallback": "defer",
        "consolidation": "defer",
        "explain_break": "defer",
        "cleanup_pass": "defer",
        "consolidation_pass": "defer",
        "smoke": "defer",
    }


def test_job_table_gives_every_job_its_family_and_an_explicit_effort(catalog):
    assert {name: (job.family, job.effort) for name, job in catalog.jobs.items()} == {
        "triage": ("glimmer", "low"),
        "triage_fallback": ("spark", "minimal"),
        "extraction": ("spark", "minimal"),
        "transcription": ("voice", None),
        "image_read": ("glimmer", "low"),
        "backfill_triage": ("spark", "minimal"),
        "backfill_extraction": ("spark", "minimal"),
        "verification": ("spark", "low"),
        "second_opinion": ("glimmer", "medium"),
        "word_judge": ("spark", "low"),
        "second_opinion_fallback": ("spark", "low"),
        "consolidation": ("spark", "high"),
        "explain_break": ("spark", "low"),
        "update_pass": ("spark", "low"),
        "cleanup_pass": ("spark", "low"),
        "consolidation_pass": ("spark", "high"),
        "smoke": ("spark", "minimal"),
    }
    assert all(job.name == name for name, job in catalog.jobs.items())


def test_price_table_is_read_from_prices_json(catalog):
    assert catalog.prices == {
        SPARK: Price(
            "tokens", input_per_million=1.25, cached_per_million=0.15, output_per_million=4.25
        ),
        VOICE: Price("audio", per_hour=0.18),
        GLIMMER: Price("gpu_seconds", per_second=0.001097),
    }


@pytest.mark.parametrize(
    "model_id",
    ["muse-spark-contributor", "Muse-Contributor-1.3", "MUSE-CONTRIBUTOR-1.0", "contributor"],
)
def test_catalog_model_refuses_a_contributor_id_even_when_it_is_declared(catalog, model_id):
    planted = ModelSpec(model_id, "spark", "meta", ("low",), True)
    with_contributor = Catalog(
        families=catalog.families,
        models={**catalog.models, model_id: planted},
        jobs=catalog.jobs,
        prices=catalog.prices,
    )

    with pytest.raises(ModelRefused) as refused:
        with_contributor.model(model_id)

    assert "contributor" in refused.value.message
    assert refused.value.status == 400


def test_catalog_model_refuses_an_unknown_id_and_lists_the_known_ones(catalog):
    with pytest.raises(ModelRefused) as refused:
        catalog.model("muse-orchard-9")

    assert refused.value.message == "unknown model"
    assert refused.value.details == {"known": [GLIMMER, SPARK, VOICE]}


def test_catalog_job_refuses_an_unknown_job(catalog):
    with pytest.raises(ModelRefused) as refused:
        catalog.job("orchard_pass")
    assert refused.value.message == "unknown job"
    assert "verification" in refused.value.details["known"]


def test_model_for_follows_the_job_family(catalog):
    assert catalog.model_for(catalog.job("triage")).id == GLIMMER
    assert catalog.model_for(catalog.job("extraction")).id == SPARK
    assert catalog.model_for(catalog.job("transcription")).id == VOICE


def test_effort_for_takes_the_job_effort_when_the_caller_gives_none(catalog):
    job = catalog.job("consolidation")
    assert catalog.effort_for(job, catalog.model(SPARK), None) == "high"
    triage = catalog.job("triage")
    assert catalog.effort_for(triage, catalog.model(GLIMMER), None) == "low"


def test_effort_for_takes_a_listed_explicit_effort_over_the_job_effort(catalog):
    job = catalog.job("verification")
    assert catalog.effort_for(job, catalog.model(SPARK), "max") == "max"
    assert catalog.effort_for(job, catalog.model(SPARK), "minimal") == "minimal"


@pytest.mark.parametrize(
    ("model_id", "effort"), [(GLIMMER, "max"), (GLIMMER, "minimal"), (SPARK, "turbo")]
)
def test_effort_for_refuses_an_effort_the_model_does_not_list(catalog, model_id, effort):
    spec = catalog.model(model_id)
    with pytest.raises(ModelRefused) as refused:
        catalog.effort_for(catalog.job("triage"), spec, effort)
    assert refused.value.details == {"efforts": spec.efforts}


def test_effort_for_refuses_a_missing_effort_on_a_model_that_takes_one(catalog):
    # Effort is explicit on every call: a job with no effort gets no silent provider default.
    no_effort = Job("orchard_pass", "spark", None, "defer")
    with pytest.raises(ModelRefused):
        catalog.effort_for(no_effort, catalog.model(SPARK), None)


@pytest.mark.parametrize("effort", ["low", "minimal", "max"])
def test_effort_for_refuses_any_effort_for_the_voice_model(catalog, effort):
    job = catalog.job("transcription")
    with pytest.raises(ModelRefused) as refused:
        catalog.effort_for(job, catalog.model(VOICE), effort)
    assert refused.value.message == "this model takes no effort"


def test_effort_for_gives_no_effort_for_the_voice_model(catalog):
    assert catalog.effort_for(catalog.job("transcription"), catalog.model(VOICE), None) is None


def test_cost_of_tokens_counts_cached_tokens_inside_the_input(catalog):
    # The probe of 2026-11-02: 7,043 input tokens of which 7,025 came from the cache.
    cost = catalog.cost(SPARK, input_tokens=7043, cached_tokens=7025, output_tokens=55)
    expected = (18 * 1.25 + 7025 * 0.15 + 55 * 4.25) / 1_000_000
    assert abs(cost - expected) < 1e-12
    assert abs(cost - 0.00131) < 1e-12


def test_cost_of_tokens_never_counts_more_cached_tokens_than_input(catalog):
    cost = catalog.cost(SPARK, input_tokens=10, cached_tokens=40, output_tokens=0)
    assert abs(cost - 10 * 0.15 / 1_000_000) < 1e-12


def test_cost_of_tokens_with_no_cache_hit(catalog):
    cost = catalog.cost(SPARK, input_tokens=1000, output_tokens=2000)
    assert abs(cost - (1000 * 1.25 + 2000 * 4.25) / 1_000_000) < 1e-12


def test_cost_of_audio_is_billed_per_hour(catalog):
    cost = catalog.cost(VOICE, audio_seconds=1250.0, input_tokens=5000, output_tokens=5000)
    assert abs(cost - 1250.0 / 3600 * 0.18) < 1e-12
    assert abs(cost - 0.0625) < 1e-12


def test_cost_of_glimmer_is_billed_per_gpu_second(catalog):
    cost = catalog.cost(GLIMMER, gpu_seconds=12.5, input_tokens=5000, output_tokens=5000)
    assert abs(cost - 12.5 * 0.001097) < 1e-12


def test_cost_refuses_a_model_with_no_price(catalog):
    with pytest.raises(ModelRefused):
        catalog.cost("muse-orchard-9", input_tokens=1)


# (1) config: settings from env.


def test_settings_defaults_with_an_empty_env():
    settings = load_model_settings({})
    assert settings == ModelSettings(
        meta_base_url=DEFAULT_META_BASE_URL,
        meta_key=None,
        glimmer_base_url=None,
        glimmer_key=None,
        daily_budget=5.0,
        gateway_secret=None,
        gateway_host="127.0.0.1",
        gateway_port=18300,
    )
    assert DEFAULT_META_BASE_URL == "https://api.meta.ai/v1"
    assert (DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT, DEFAULT_DAILY_BUDGET) == (
        "127.0.0.1",
        18300,
        5.0,
    )


@pytest.mark.parametrize(
    ("value", "budget"),
    [("unlimited", None), ("2.5", 2.5), ("0", 0.0), ("12", 12.0), ("", 5.0)],
)
def test_settings_daily_budget(value, budget):
    assert load_model_settings({"GROVE_DAILY_BUDGET_USD": value}).daily_budget == budget


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf", "Unlimited", "2.5usd"])
def test_settings_refuse_a_daily_budget_that_is_not_a_finite_number(value):
    with pytest.raises(NotConfigured) as refused:
        load_model_settings({"GROVE_DAILY_BUDGET_USD": value})
    assert "GROVE_DAILY_BUDGET_USD" in refused.value.message
    assert value not in refused.value.message


def test_settings_read_the_process_env_when_no_env_is_given(monkeypatch):
    monkeypatch.setenv("GROVE_DAILY_BUDGET_USD", "2.5")
    monkeypatch.setenv("GROVE_GATEWAY_PORT", "18411")
    monkeypatch.delenv("GROVE_META_BASE_URL", raising=False)
    monkeypatch.delenv("GROVE_GLIMMER_BASE_URL", raising=False)
    settings = load_model_settings()
    assert settings.daily_budget == 2.5
    assert settings.gateway_port == 18411


def test_settings_take_every_variable():
    settings = load_model_settings(
        {
            "GROVE_META_BASE_URL": "https://api.meta.ai/v1/",
            "MODEL_API_KEY": "test-meta-key",
            "GROVE_GLIMMER_BASE_URL": f"https://{MODAL_HOST}/v1",
            "GROVE_GLIMMER_KEY": "test-glimmer-key",
            "GROVE_GATEWAY_SECRET": "k" * 32,
            "GROVE_GATEWAY_HOST": "127.0.0.2",
            "GROVE_GATEWAY_PORT": "18411",
        }
    )
    assert settings.meta_base_url == "https://api.meta.ai/v1"
    assert settings.require_meta_key() == "test-meta-key"
    assert settings.require_glimmer() == (f"https://{MODAL_HOST}/v1", "test-glimmer-key")
    assert settings.require_gateway_secret() == b"k" * 32
    assert (settings.gateway_host, settings.gateway_port) == ("127.0.0.2", 18411)
    assert settings.model_hosts() == frozenset({META_HOST, MODAL_HOST})


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("GROVE_META_BASE_URL", "https://api.meta.ai.example.net/v1"),
        ("GROVE_META_BASE_URL", "https://203.0.113.9/v1"),
        ("GROVE_META_BASE_URL", "https://api.meta.ai:8443/v1"),
        ("GROVE_GLIMMER_BASE_URL", "https://glimmer.example.test/v1"),
        ("GROVE_GLIMMER_BASE_URL", "https://modal.direct/v1"),
        ("GROVE_GLIMMER_BASE_URL", "https://ws--app.eu-west.modal.direct.example.net/v1"),
    ],
)
def test_settings_refuse_a_base_url_off_the_model_hosts(name, url):
    # The allowlist comes from these URLs and the keys go to them: a planted URL must not load.
    with pytest.raises(NotConfigured, match=name):
        load_model_settings({name: url})


@pytest.mark.parametrize(
    "url",
    [
        "http://api.meta.ai/v1",
        "ftp://api.meta.ai/v1",
        "https:///v1",
        "https://lio:orchard-pw@api.meta.ai/v1",
        "https://lio@api.meta.ai/v1",
    ],
)
@pytest.mark.parametrize("name", ["GROVE_META_BASE_URL", "GROVE_GLIMMER_BASE_URL"])
def test_settings_refuse_a_base_url_that_is_not_plain_https(name, url):
    with pytest.raises(NotConfigured) as refused:
        load_model_settings({name: url})
    assert name in refused.value.message
    assert "orchard-pw" not in json.dumps(refused.value.as_body())


def test_settings_repr_holds_no_key_and_no_secret():
    shown = repr(SETTINGS)
    assert "test-meta-key" not in shown
    assert "test-glimmer-key" not in shown
    assert "sssss" not in shown


@pytest.mark.parametrize("secret", [None, b"", b"s" * 31])
def test_require_gateway_secret_refuses_under_32_bytes(secret):
    with pytest.raises(NotConfigured):
        ModelSettings(gateway_secret=secret).require_gateway_secret()


def test_require_gateway_secret_takes_32_bytes():
    assert ModelSettings(gateway_secret=b"s" * 32).require_gateway_secret() == b"s" * 32


def test_require_meta_key_and_glimmer_name_the_missing_variable():
    with pytest.raises(NotConfigured, match="MODEL_API_KEY"):
        ModelSettings().require_meta_key()
    with pytest.raises(NotConfigured, match="GROVE_GLIMMER_BASE_URL"):
        ModelSettings(glimmer_key="test-glimmer-key").require_glimmer()
    with pytest.raises(NotConfigured, match="GROVE_GLIMMER_KEY"):
        ModelSettings(glimmer_base_url="https://glimmer.example.test/v1").require_glimmer()


# (1) config: the loader refuses a planted document.


def _plant(monkeypatch, change) -> None:
    """Hand load_catalog the shipped documents after `change(jobs_doc, prices_doc)`."""
    docs = {name: copy.deepcopy(config._read_json(name)) for name in ("jobs.json", "prices.json")}
    change(docs["jobs.json"], docs["prices.json"])
    monkeypatch.setattr(config, "_read_json", lambda name: docs[name])


def test_loader_reads_the_planted_documents(monkeypatch):
    def change(jobs, prices):
        prices["models"][SPARK]["output_per_million"] = 9.0

    _plant(monkeypatch, change)
    assert load_catalog().prices[SPARK].output_per_million == 9.0


@pytest.mark.parametrize("model_id", ["muse-spark-contributor-1.3", "Muse-Spark-CONTRIBUTOR"])
def test_loader_refuses_a_contributor_model_declared_in_the_job_table(monkeypatch, model_id):
    def change(jobs, prices):
        jobs["models"][model_id] = dict(jobs["models"][SPARK])
        prices["models"][model_id] = dict(prices["models"][SPARK])

    _plant(monkeypatch, change)
    with pytest.raises(ModelRefused, match="contributor"):
        load_catalog()


def test_loader_refuses_a_family_that_points_at_a_contributor_id(monkeypatch):
    def change(jobs, prices):
        jobs["families"]["spark"] = "muse-spark-contributor-1.3"

    _plant(monkeypatch, change)
    with pytest.raises(ModelRefused, match="contributor"):
        load_catalog()


def test_loader_refuses_a_contributor_id_in_the_price_table(monkeypatch):
    def change(jobs, prices):
        prices["models"]["muse-spark-contributor-1.3"] = dict(prices["models"][SPARK])

    _plant(monkeypatch, change)
    with pytest.raises(ModelRefused, match="contributor"):
        load_catalog()


@pytest.mark.parametrize(
    ("job", "effort"),
    [
        ("triage", "max"),
        ("triage", "minimal"),
        ("transcription", "low"),
        ("verification", None),
        ("extraction", "turbo"),
    ],
)
def test_loader_refuses_a_job_whose_effort_does_not_fit_its_model(monkeypatch, job, effort):
    def change(jobs, prices):
        jobs["jobs"][job]["effort"] = effort

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match=f"job {job}: its effort does not fit its model"):
        load_catalog()


def test_loader_refuses_an_unknown_at_ceiling(monkeypatch):
    def change(jobs, prices):
        jobs["jobs"]["verification"]["at_ceiling"] = "skip"

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match="job verification: at_ceiling"):
        load_catalog()


def test_loader_refuses_a_job_of_an_unknown_family(monkeypatch):
    def change(jobs, prices):
        jobs["jobs"]["verification"]["family"] = "orchard"

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match="job verification: unknown family"):
        load_catalog()


def test_loader_refuses_an_unknown_route(monkeypatch):
    def change(jobs, prices):
        jobs["models"][GLIMMER]["route"] = "orchard"

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match="unknown route"):
        load_catalog()


def test_loader_refuses_an_effort_outside_the_effort_enum(monkeypatch):
    def change(jobs, prices):
        jobs["models"][GLIMMER]["efforts"] = ["low", "medium", "high", "turbo"]

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match="outside the effort enum"):
        load_catalog()


def test_loader_refuses_a_family_whose_model_sits_under_another_family(monkeypatch):
    def change(jobs, prices):
        jobs["families"]["glimmer"] = SPARK

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match="family glimmer"):
        load_catalog()


def test_loader_refuses_a_model_with_no_price(monkeypatch):
    def change(jobs, prices):
        del prices["models"][GLIMMER]

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match="models with no price"):
        load_catalog()


@pytest.mark.parametrize("value", [-0.1, True, "1.25", float("nan"), None])
def test_loader_refuses_a_price_that_is_not_a_finite_non_negative_number(monkeypatch, value):
    def change(jobs, prices):
        prices["models"][SPARK]["input_per_million"] = value

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match=f"price {SPARK} input_per_million"):
        load_catalog()


def test_loader_refuses_an_unknown_price_basis(monkeypatch):
    def change(jobs, prices):
        prices["models"][VOICE]["basis"] = "minutes"

    _plant(monkeypatch, change)
    with pytest.raises(ValueError, match="unknown basis"):
        load_catalog()


# (2) egress.


@pytest.fixture
def policy() -> EgressPolicy:
    return EgressPolicy.for_settings(SETTINGS)


def test_runtimes_are_the_layer_and_the_four_agents():
    assert RUNTIMES == ("model_layer", "grove", "learner", "curator", "connector")
    assert AGENTS == ("grove", "learner", "curator", "connector")
    assert CLEANUP_AGENT == "curator"


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_every_runtime_reaches_meta_and_glimmer(policy, runtime):
    assert policy.hosts(runtime) == frozenset({META_HOST, GLIMMER_HOST})
    policy.check(runtime, "https://api.meta.ai/v1/responses")
    policy.check(runtime, "https://glimmer.example.test/v1/chat/completions")
    policy.check(runtime, "https://api.meta.ai:443/v1/responses")
    policy.check(runtime, "https://API.Meta.AI./v1/responses")


def test_glimmer_host_is_off_the_list_when_glimmer_is_not_configured():
    meta_only = EgressPolicy.for_settings(ModelSettings(meta_key="test-meta-key"))
    assert meta_only.hosts(MODEL_LAYER) == frozenset({META_HOST})
    with pytest.raises(EgressRefused):
        meta_only.check(MODEL_LAYER, "https://glimmer.example.test/v1/chat/completions")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://api.meta.ai/v1/responses", "only https leaves the box"),
        ("ws://api.meta.ai/v1/responses", "only https leaves the box"),
        ("https://api.meta.ai:8443/v1/responses", "only the https port is allowed"),
        ("https://api.meta.ai:80/v1/responses", "only the https port is allowed"),
        ("https://api.meta.ai:abc/v1/responses", "only the https port is allowed"),
        ("https://lio@api.meta.ai/v1/responses", "a URL with a user part is refused"),
        ("https://lio:pw@api.meta.ai/v1/responses", "a URL with a user part is refused"),
        ("https://orchard.example.test/v1", "host not on the egress allowlist"),
        ("https://api.meta.ai.orchard.example.test/v1", "host not on the egress allowlist"),
        ("https://evilapi.meta.ai/v1", "host not on the egress allowlist"),
        ("https:///v1", "host not on the egress allowlist"),
    ],
)
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_check_refuses_a_url_off_the_list(policy, runtime, url, message):
    with pytest.raises(EgressRefused) as refused:
        policy.check(runtime, url)
    assert refused.value.message == message
    assert refused.value.details["runtime"] == runtime
    assert refused.value.status == 403


@pytest.mark.parametrize(
    ("url", "host"),
    [("https://93.184.216.34/v1", "93.184.216.34"), ("https://[2001:db8::1]/v1", "2001:db8::1")],
)
def test_check_refuses_an_ip_literal_and_says_so(policy, url, host):
    with pytest.raises(EgressRefused) as refused:
        policy.check(MODEL_LAYER, url)
    assert refused.value.details == {"runtime": MODEL_LAYER, "host": host, "ip_literal": True}


def test_check_refuses_an_unknown_runtime(policy):
    with pytest.raises(EgressRefused, match="unknown runtime"):
        policy.check("orchard", "https://api.meta.ai/v1/responses")


def test_policy_refuses_an_unknown_runtime_name():
    with pytest.raises(ValueError, match="unknown runtimes"):
        EgressPolicy({"orchard": ["api.meta.ai"]})


def test_connector_hosts_reach_only_the_clean_up_agent():
    with_connectors = EgressPolicy.for_settings(
        SETTINGS, connector_hosts=["Drive.Example.Test", "mail.example.test"]
    )
    url = "https://drive.example.test/files"
    with_connectors.check("curator", url)
    with_connectors.check("curator", "https://mail.example.test/inbox")
    assert with_connectors.hosts("curator") == frozenset(
        {META_HOST, GLIMMER_HOST, "drive.example.test", "mail.example.test"}
    )
    for runtime in (MODEL_LAYER, "grove", "learner", "connector"):
        assert with_connectors.hosts(runtime) == frozenset({META_HOST, GLIMMER_HOST})
        with pytest.raises(EgressRefused):
            with_connectors.check(runtime, url)


def _counting_inner():
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler), hits


def test_allowlist_transport_refuses_before_the_inner_transport_is_called(policy):
    inner, hits = _counting_inner()
    with httpx.Client(transport=AllowlistTransport(policy, MODEL_LAYER, inner=inner)) as client:
        for url in (
            "https://orchard.example.test/v1",
            "http://api.meta.ai/v1/responses",
            "https://api.meta.ai:8443/v1/responses",
            "https://93.184.216.34/v1",
        ):
            with pytest.raises(EgressRefused):
                client.get(url)
        assert hits == []
        assert client.get("https://api.meta.ai/v1/models").status_code == 200
    assert hits == ["https://api.meta.ai/v1/models"]


def test_allowlist_transport_refuses_an_unknown_runtime(policy):
    inner, _ = _counting_inner()
    with pytest.raises(EgressRefused):
        AllowlistTransport(policy, "orchard", inner=inner)


@pytest.mark.parametrize(
    "location", ["https://orchard.example.test/steal", "http://api.meta.ai/v1/models"]
)
def test_a_redirect_off_the_list_is_refused_on_the_second_hop(policy, location):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.path == "/v1/models":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, json={"stolen": True})

    transport = AllowlistTransport(policy, MODEL_LAYER, inner=httpx.MockTransport(handler))
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        with pytest.raises(EgressRefused):
            client.get("https://api.meta.ai/v1/models")
    assert hits == ["https://api.meta.ai/v1/models"]


def test_a_redirect_within_the_list_is_followed(policy):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.host == META_HOST:
            return httpx.Response(302, headers={"location": "https://glimmer.example.test/v1/x"})
        return httpx.Response(200, json={"ok": True})

    transport = AllowlistTransport(policy, MODEL_LAYER, inner=httpx.MockTransport(handler))
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        assert client.get("https://api.meta.ai/v1/models").json() == {"ok": True}
    assert hits == ["https://api.meta.ai/v1/models", "https://glimmer.example.test/v1/x"]


def test_async_allowlist_transport_refuses_before_the_inner_transport_is_called(policy):
    hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    async def run() -> int:
        inner = httpx.MockTransport(handler)
        transport = AsyncAllowlistTransport(policy, "grove", inner=inner)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(EgressRefused):
                await client.get("https://orchard.example.test/v1")
            assert hits == []
            response = await client.get("https://glimmer.example.test/v1/models")
            return response.status_code

    assert asyncio.run(run()) == 200
    assert hits == ["https://glimmer.example.test/v1/models"]


# (3) http: the stated wait and the back-off.


@pytest.mark.parametrize(
    ("headers", "wait"),
    [
        ({"retry-after": "2"}, 2.0),
        ({"retry-after": "1.5"}, 1.5),
        ({"retry-after": "0"}, 0.0),
        ({"retry-after": "-5"}, 0.0),
        ({"x-ratelimit-reset-requests": "6m0s"}, 360.0),
        ({"x-ratelimit-reset-requests": "250ms"}, 0.25),
        ({"x-ratelimit-reset-requests": "1h2m3s"}, 3723.0),
        ({"x-ratelimit-reset-tokens": "2m30s"}, 150.0),
        ({"x-ratelimit-reset-tokens": "1m"}, 60.0),
        ({"x-ratelimit-reset-requests": "2s", "x-ratelimit-reset-tokens": "7.5s"}, 7.5),
        ({"retry-after": "soon", "x-ratelimit-reset-tokens": "3s"}, 3.0),
        ({"retry-after": "4", "x-ratelimit-reset-tokens": "30s"}, 4.0),
        ({"retry-after": "soon"}, None),
        ({"x-ratelimit-reset-requests": "6 minutes"}, None),
        ({"x-ratelimit-reset-requests": "ms"}, None),
        ({}, None),
    ],
)
def test_reset_after_reads_the_stated_wait(headers, wait):
    assert reset_after(httpx.Response(429, headers=headers)) == wait


@pytest.mark.parametrize(
    "headers",
    [
        {"retry-after": "nan"},
        {"x-ratelimit-reset-requests": "-5"},
        {"x-ratelimit-reset-tokens": "NaN"},
    ],
)
def test_reset_after_never_states_a_negative_or_nan_wait(headers):
    wait = reset_after(httpx.Response(429, headers=headers))
    assert wait is None or (math.isfinite(wait) and wait >= 0)


def _provider(steps: list):
    """A fake provider: each step is (status, headers) or "drop"; the last step repeats."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        step = steps[min(len(seen), len(steps)) - 1]
        if step == "drop":
            raise httpx.ConnectError("connection refused", request=request)
        status, headers = step
        return httpx.Response(status, headers=headers, json={"attempt": len(seen)})

    return answer, seen


def _send(steps: list, **kwargs):
    answer, seen = _provider(steps)
    sleeps: list[float] = []
    builds: list[int] = []
    with httpx.Client(transport=httpx.MockTransport(answer)) as client:

        def build() -> httpx.Request:
            builds.append(1)
            return client.build_request(
                "POST", "https://api.meta.ai/v1/responses", json={"input": "orchard"}
            )

        try:
            response = send(client, build, sleep=sleeps.append, **kwargs)
        except Exception as error:  # noqa: BLE001 - the test reads it back
            return error, seen, sleeps, len(builds)
    return response, seen, sleeps, len(builds)


def test_send_waits_the_stated_retry_after_on_a_429():
    response, seen, sleeps, builds = _send([(429, {"retry-after": "3"}), (200, {})])
    assert response.status_code == 200
    assert response.json() == {"attempt": 2}
    assert sleeps == [3.0]
    assert len(seen) == builds == 2
    assert all(request.content == b'{"input":"orchard"}' for request in seen)


def test_send_waits_the_rate_limit_reset_header_on_a_429():
    steps = [
        (429, {"x-ratelimit-reset-requests": "1.5s"}),
        (429, {"retry-after": "0.25"}),
        (200, {}),
    ]
    response, _, sleeps, _ = _send(steps)
    assert response.status_code == 200
    assert sleeps == [1.5, 0.25]


def test_send_backs_off_by_doubling_when_a_429_states_no_wait():
    response, _, sleeps, _ = _send([(429, {}), (429, {}), (200, {})])
    assert response.status_code == 200
    assert sleeps == [2.0, 4.0]


def test_send_returns_a_429_whose_reset_is_too_far_to_wait_for():
    # Nothing goes out again before the stated reset, and no worker sleeps minutes on one call.
    response, seen, sleeps, _ = _send([(429, {"retry-after": "120"}), (200, {})])
    assert response.status_code == 429
    assert len(seen) == 1
    assert sleeps == []
    assert MAX_WAIT == 60.0


def test_send_waits_a_stated_reset_up_to_max_wait():
    response, _, sleeps, _ = _send([(429, {"retry-after": "60"}), (200, {})])
    assert response.status_code == 200
    assert sleeps == [60.0]


@pytest.mark.parametrize("status", [500, 502, 504])
def test_send_never_resends_a_call_that_may_have_run(status):
    response, seen, sleeps, _ = _send([(status, {}), (200, {})])
    assert response.status_code == status
    assert len(seen) == 1
    assert sleeps == []


def test_send_returns_the_last_429_after_max_attempts():
    response, seen, sleeps, builds = _send([(429, {"retry-after": "5"})])
    assert response.status_code == 429
    assert response.json() == {"attempt": MAX_ATTEMPTS}
    assert reset_after(response) == 5.0
    assert len(seen) == builds == MAX_ATTEMPTS == 4
    assert sleeps == [5.0] * (MAX_ATTEMPTS - 1)


def test_send_retries_a_503():
    response, seen, sleeps, _ = _send([(503, {}), (200, {})])
    assert response.status_code == 200
    assert len(seen) == 2
    assert sleeps == [2.0]


def test_send_returns_the_last_503_after_max_attempts():
    response, seen, sleeps, _ = _send([(503, {"retry-after": "1"})])
    assert response.status_code == 503
    assert len(seen) == MAX_ATTEMPTS
    # A 503 backs off by doubling; only a 429 waits the stated time.
    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_send_does_not_retry_a_client_error(status):
    response, seen, sleeps, _ = _send([(status, {}), (200, {})])
    assert response.status_code == status
    assert len(seen) == 1
    assert sleeps == []


def test_send_retries_a_dropped_connection():
    response, seen, sleeps, _ = _send(["drop", "drop", (200, {})])
    assert response.status_code == 200
    assert len(seen) == 3
    assert sleeps == [2.0, 4.0]


def test_send_raises_the_transport_error_after_the_last_attempt():
    error, seen, sleeps, builds = _send(["drop"])
    assert isinstance(error, httpx.ConnectError)
    assert len(seen) == builds == MAX_ATTEMPTS
    assert sleeps == [2.0, 4.0, 8.0]


def test_send_does_not_retry_an_egress_refusal(policy):
    inner, hits = _counting_inner()
    sleeps: list[float] = []
    builds: list[int] = []
    transport = AllowlistTransport(policy, MODEL_LAYER, inner=inner)
    with httpx.Client(transport=transport) as client:

        def build() -> httpx.Request:
            builds.append(1)
            return client.build_request("POST", "https://orchard.example.test/v1/responses")

        with pytest.raises(EgressRefused):
            send(client, build, sleep=sleeps.append)
    assert builds == [1]
    assert hits == []
    assert sleeps == []


def test_send_with_modal_attempts_keeps_retrying_a_cold_start_503():
    steps = [(503, {})] * (MODAL_ATTEMPTS - 1) + [(200, {})]
    response, seen, sleeps, _ = _send(steps, attempts=MODAL_ATTEMPTS)
    assert response.status_code == 200
    assert response.json() == {"attempt": MODAL_ATTEMPTS}
    assert len(seen) == MODAL_ATTEMPTS
    assert sleeps == [min(2.0**attempt, MAX_WAIT) for attempt in range(1, MODAL_ATTEMPTS)]
    assert MODAL_ATTEMPTS > MAX_ATTEMPTS
    # A scaled-to-zero server needs minutes for a container: the waits must cover that.
    assert sum(sleeps) >= 300


def test_send_with_modal_attempts_returns_the_503_when_no_container_comes_up():
    response, seen, sleeps, _ = _send([(503, {})], attempts=MODAL_ATTEMPTS)
    assert response.status_code == 503
    assert len(seen) == MODAL_ATTEMPTS
    assert len(sleeps) == MODAL_ATTEMPTS - 1


@pytest.mark.parametrize("headers", [{"retry-after": "nan"}, {"x-ratelimit-reset-requests": "-5"}])
def test_send_never_hands_sleep_a_negative_or_nan_wait(headers):
    # time.sleep raises ValueError on a negative or a NaN length, so the call would crash.
    response, _, sleeps, _ = _send([(429, headers), (200, {})])
    assert getattr(response, "status_code", None) == 200
    assert len(sleeps) == 1
    assert math.isfinite(sleeps[0]) and 0 <= sleeps[0] <= MAX_WAIT


def _asend(steps: list, **kwargs):
    answer, seen = _provider(steps)
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return answer(request)

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            def build() -> httpx.Request:
                return client.build_request("POST", "https://glimmer.example.test/v1/x", json={})

            try:
                response = await asend(client, build, sleep=record, **kwargs)
            except Exception as error:  # noqa: BLE001 - the test reads it back
                return error, None
            body = json.loads(await response.aread())
            await response.aclose()
            return response, body

    result, body = asyncio.run(run())
    return result, body, seen, sleeps


def test_asend_waits_the_stated_retry_after_with_an_async_sleep():
    response, body, seen, sleeps = _asend([(429, {"retry-after": "1.5"}), (200, {})])
    assert response.status_code == 200
    assert body == {"attempt": 2}
    assert sleeps == [1.5]
    assert len(seen) == 2


def test_asend_streams_and_retries_a_503():
    response, body, seen, sleeps = _asend([(503, {}), (503, {}), (200, {})], stream=True)
    assert response.status_code == 200
    assert body == {"attempt": 3}
    assert sleeps == [2.0, 4.0]


def test_asend_returns_the_last_429_after_max_attempts():
    response, body, seen, sleeps = _asend([(429, {})])
    assert response.status_code == 429
    assert body == {"attempt": MAX_ATTEMPTS}
    assert sleeps == [2.0, 4.0, 8.0]


def test_asend_keeps_retrying_a_503_for_modal_attempts():
    steps = [(503, {})] * (MODAL_ATTEMPTS - 1) + [(200, {})]
    response, _, seen, sleeps = _asend(steps, attempts=MODAL_ATTEMPTS)
    assert response.status_code == 200
    assert len(seen) == MODAL_ATTEMPTS
    assert sleeps == [min(2.0**attempt, MAX_WAIT) for attempt in range(1, MODAL_ATTEMPTS)]


def test_asend_raises_the_transport_error_after_the_last_attempt():
    error, _, seen, sleeps = _asend(["drop"])
    assert isinstance(error, httpx.ConnectError)
    assert len(seen) == MAX_ATTEMPTS
    assert sleeps == [2.0, 4.0, 8.0]


def test_asend_does_not_retry_a_client_error():
    response, _, seen, sleeps = _asend([(400, {}), (200, {})])
    assert response.status_code == 400
    assert len(seen) == 1
    assert sleeps == []


# (4) audio.


def _wav(seconds: int, *, rate: int = 1000, channels: int = 1, width: int = 1) -> bytes:
    size = seconds * rate * channels * width
    frames = bytes(index % 251 for index in range(size))
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return out.getvalue()


def _frames(data: bytes) -> tuple[bytes, tuple[int, int, int]]:
    with wave.open(io.BytesIO(data), "rb") as reader:
        shape = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
        return reader.readframes(reader.getnframes()), shape


@pytest.mark.parametrize(
    ("rate", "channels", "width"), [(1000, 1, 1), (100, 2, 2)], ids=["mono-8bit", "stereo-16bit"]
)
def test_split_wav_cuts_long_audio_into_complete_wav_pieces(rate, channels, width):
    data = _wav(1250, rate=rate, channels=channels, width=width)
    assert wav_seconds(data) == 1250.0

    pieces = split_wav(data)

    assert [piece.seconds for piece in pieces] == [590.0, 590.0, 70.0]
    assert all(piece.seconds <= PIECE_SECONDS < API_LIMIT_SECONDS for piece in pieces)
    assert sum(piece.seconds for piece in pieces) == 1250.0
    joined = b""
    for piece in pieces:
        assert wav_seconds(piece.data) == piece.seconds
        frames, shape = _frames(piece.data)
        assert shape == (channels, width, rate)
        joined += frames
    assert joined == _frames(data)[0]


def test_split_wav_keeps_short_audio_in_one_piece():
    data = _wav(590)
    pieces = split_wav(data)
    assert [piece.seconds for piece in pieces] == [590.0]
    assert _frames(pieces[0].data)[0] == _frames(data)[0]


@pytest.mark.parametrize(
    "data", [b"", b"not a wav file", b"RIFF\x00\x00\x00\x00WAVE", b"\x00" * 64]
)
def test_split_wav_refuses_audio_that_is_not_a_wav(data):
    with pytest.raises(ModelRefused):
        split_wav(data)
    with pytest.raises(ModelRefused):
        wav_seconds(data)


def test_split_wav_refuses_a_wav_with_no_frames():
    with pytest.raises(ModelRefused, match="no frames"):
        split_wav(_wav(0))


@pytest.mark.parametrize("piece_seconds", [0, -1, API_LIMIT_SECONDS + 1])
def test_split_wav_refuses_a_piece_length_outside_the_api_limit(piece_seconds):
    with pytest.raises(AudioTooLong):
        split_wav(_wav(2), piece_seconds=piece_seconds)


# (5) gateway_auth: run tokens.


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _forge(secret: bytes, claims: object) -> str:
    """A run token signed the way the gateway signs, for claims the minter would not write."""
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret, f"g1.{payload}".encode("ascii"), hashlib.sha256).digest()
    return f"g1.{payload}.{_b64(signature)}"


@pytest.fixture
def frozen_clock():
    at = TestClock()
    at.set(T0)
    with clock.using(at):
        yield at


def test_mint_and_verify_round_trip(frozen_clock, catalog, world):
    token = mint(
        SECRET,
        catalog,
        world_id=world,
        run_id="run-orchard-1",
        job="verification",
        ttl_seconds=600,
        token_cap=5000,
    )

    assert token.startswith("g1.")
    assert len(token.split(".")) == 3
    assert verify(SECRET, catalog, token) == RunClaims(
        world_id=world,
        run_id="run-orchard-1",
        job="verification",
        expires=int(T0.timestamp()) + 600,
        token_cap=5000,
    )


def test_mint_writes_the_documented_form(frozen_clock, catalog, world):
    token = mint(SECRET, catalog, world_id=world, run_id="run-1", job="triage")
    claims = {"w": world, "r": "run-1", "j": "triage", "exp": int(T0.timestamp()) + 3600}
    assert token == _forge(SECRET, claims)
    assert verify(SECRET, catalog, token).token_cap is None


def test_verify_refuses_a_changed_payload(frozen_clock, catalog, world):
    token = mint(SECRET, catalog, world_id=world, run_id="run-1", job="verification")
    prefix, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["j"] = "consolidation"
    changed = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))

    with pytest.raises(Unauthenticated, match="signature"):
        verify(SECRET, catalog, f"{prefix}.{changed}.{signature}")


def test_verify_refuses_a_changed_signature(frozen_clock, catalog, world):
    token = mint(SECRET, catalog, world_id=world, run_id="run-1", job="verification")
    swap = "A" if token[-1] != "A" else "B"
    with pytest.raises(Unauthenticated, match="signature"):
        verify(SECRET, catalog, token[:-1] + swap)
    with pytest.raises(Unauthenticated):
        verify(SECRET, catalog, token.rsplit(".", 1)[0] + ".")


def test_verify_refuses_a_token_signed_with_another_secret(frozen_clock, catalog, world):
    token = mint(b"t" * 32, catalog, world_id=world, run_id="run-1", job="verification")
    with pytest.raises(Unauthenticated, match="signature"):
        verify(SECRET, catalog, token)


def test_verify_refuses_an_expired_token(frozen_clock, catalog, world):
    token = mint(
        SECRET, catalog, world_id=world, run_id="run-1", job="verification", ttl_seconds=600
    )
    frozen_clock.advance(seconds=599)
    assert verify(SECRET, catalog, token).run_id == "run-1"

    frozen_clock.advance(seconds=1)
    with pytest.raises(Unauthenticated, match="expired"):
        verify(SECRET, catalog, token)
    frozen_clock.advance(hours=2)
    with pytest.raises(Unauthenticated, match="expired"):
        verify(SECRET, catalog, token)


def test_mint_takes_the_longest_ttl_and_refuses_one_over_it(frozen_clock, catalog, world):
    token = mint(
        SECRET, catalog, world_id=world, run_id="r", job="smoke", ttl_seconds=MAX_TTL_SECONDS
    )
    assert verify(SECRET, catalog, token).expires == int(T0.timestamp()) + MAX_TTL_SECONDS
    assert MAX_TTL_SECONDS == 6 * 3600
    for ttl in (MAX_TTL_SECONDS + 1, 0, -60):
        with pytest.raises(ModelRefused, match="ttl_seconds"):
            mint(SECRET, catalog, world_id=world, run_id="r", job="smoke", ttl_seconds=ttl)


def test_mint_refuses_an_unknown_job(frozen_clock, catalog, world):
    with pytest.raises(ModelRefused, match="unknown job"):
        mint(SECRET, catalog, world_id=world, run_id="r", job="orchard_pass")


@pytest.mark.parametrize("world_id", ["orchard", "", "t_models_not-a-uuid", "T_MODELS_X"])
def test_mint_refuses_a_bad_world_id(frozen_clock, catalog, world_id):
    with pytest.raises(ScopeError):
        mint(SECRET, catalog, world_id=world_id, run_id="r", job="verification")


@pytest.mark.parametrize("run_id", ["", "r" * 81, "run-été"])
def test_mint_refuses_a_bad_run_id(frozen_clock, catalog, world, run_id):
    with pytest.raises(ModelRefused, match="run_id"):
        mint(SECRET, catalog, world_id=world, run_id=run_id, job="verification")


@pytest.mark.parametrize("token_cap", [0, -5, True])
def test_mint_refuses_a_token_cap_that_is_not_positive(frozen_clock, catalog, world, token_cap):
    with pytest.raises(ModelRefused, match="token_cap"):
        mint(SECRET, catalog, world_id=world, run_id="r", job="verification", token_cap=token_cap)


def test_mint_refuses_a_token_cap_that_is_not_an_integer(frozen_clock, catalog, world):
    # The refusal says "a positive integer"; a fraction of a token is no cap a run can use.
    with pytest.raises(ModelRefused, match="token_cap"):
        mint(SECRET, catalog, world_id=world, run_id="r", job="verification", token_cap=2.5)


def test_verify_refuses_signed_claims_with_a_bad_world_id(frozen_clock, catalog):
    claims = {"w": "orchard", "r": "r", "j": "verification", "exp": int(T0.timestamp()) + 60}
    with pytest.raises(Unauthenticated, match="malformed"):
        verify(SECRET, catalog, _forge(SECRET, claims))


def test_verify_refuses_signed_claims_with_an_unknown_job(frozen_clock, catalog, world):
    claims = {"w": world, "r": "r", "j": "orchard_pass", "exp": int(T0.timestamp()) + 60}
    with pytest.raises(Unauthenticated, match="no known job"):
        verify(SECRET, catalog, _forge(SECRET, claims))


@pytest.mark.parametrize(
    "claims",
    [
        {"r": "r", "j": "verification", "exp": 1},
        ["w", "r"],
        "orchard",
    ],
)
def test_verify_refuses_signed_claims_of_the_wrong_shape(frozen_clock, catalog, claims):
    with pytest.raises(Unauthenticated, match="malformed"):
        verify(SECRET, catalog, _forge(SECRET, claims))


def test_verify_refuses_signed_claims_with_an_expiry_that_is_not_an_integer(
    frozen_clock, catalog, world
):
    future = str(int(T0.timestamp()) + 3600)
    claims = {"w": world, "r": "r", "j": "verification", "exp": future}
    with pytest.raises(Unauthenticated, match="expired"):
        verify(SECRET, catalog, _forge(SECRET, claims))


def test_verify_refuses_signed_claims_with_a_run_id_that_is_not_text(frozen_clock, catalog, world):
    claims = {"w": world, "r": 7, "j": "verification", "exp": int(T0.timestamp()) + 60}
    with pytest.raises(Unauthenticated):
        verify(SECRET, catalog, _forge(SECRET, claims))


@pytest.mark.parametrize("token", ["", "orchard", "g2.a.b", "g1.a", "g1.a.b.c", None, 42])
def test_verify_refuses_what_is_not_a_run_token(frozen_clock, catalog, token):
    with pytest.raises(Unauthenticated):
        verify(SECRET, catalog, token)


def test_verify_refuses_a_signed_payload_that_is_not_base64_json(frozen_clock, catalog):
    payload = "!!not-json!!"
    signature = hmac.new(SECRET, f"g1.{payload}".encode("ascii"), hashlib.sha256).digest()
    with pytest.raises(Unauthenticated, match="malformed"):
        verify(SECRET, catalog, f"g1.{payload}.{_b64(signature)}")


# (6) layer helpers.


def _chain(levels: int, *, enum: bool = False) -> dict:
    """An object schema `levels` objects deep."""
    node: dict = {"type": "string"}
    if enum:
        node["enum"] = ["orchard"]
    for level in range(levels, 0, -1):
        node = {"type": "object", "properties": {f"a{level}": node}}
    return node


def test_check_schema_takes_a_flat_object_schema_and_validates_with_it():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["name", "count"],
        "additionalProperties": False,
    }
    validator = check_schema(schema)
    assert validator.is_valid({"name": "Lio Varn", "count": 2})
    assert not validator.is_valid({"name": "Lio Varn"})


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"$ref": "#/$defs/b"}}},
        {"type": "object", "$defs": {"b": {"type": "string"}}},
        {"type": "object", "definitions": {"b": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"anyOf": [{"$ref": "#"}]}}},
    ],
)
def test_check_schema_refuses_references(schema):
    with pytest.raises(ModelRefused, match="references"):
        check_schema(schema)


def test_check_schema_takes_ten_levels_and_refuses_eleven():
    assert MAX_SCHEMA_DEPTH == 10
    # The limit counts nested schemas, as Meta's does, not the JSON wrappers between them.
    check_schema(_chain(10, enum=True))
    with pytest.raises(ModelRefused, match="nest at most 10 levels"):
        check_schema(_chain(11))
    with pytest.raises(ModelRefused, match="nest at most 10 levels"):
        check_schema(_chain(12))


@pytest.mark.parametrize(
    "schema",
    [{"type": "array", "items": {"type": "string"}}, {"properties": {}}, [], "object", None],
)
def test_check_schema_refuses_a_root_that_is_not_an_object_schema(schema):
    with pytest.raises(ModelRefused, match="object schema"):
        check_schema(schema)


def test_check_schema_refuses_too_many_properties():
    def many(count: int) -> dict:
        return {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(count)}}

    check_schema(many(MAX_SCHEMA_PROPERTIES))
    with pytest.raises(ModelRefused, match="at most 5000 properties"):
        check_schema(many(MAX_SCHEMA_PROPERTIES + 1))


def test_check_schema_refuses_an_invalid_json_schema():
    with pytest.raises(ModelRefused, match="not a valid JSON schema"):
        check_schema({"type": "object", "properties": {"a": {"type": 12}}})


_ITEMS = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {"n": {"type": "integer"}}},
        },
    },
    "required": ["name"],
}


def test_parse_structured_returns_the_data_that_fits():
    data = parse_structured('{"name": "orchard", "items": [{"n": 1}]}', check_schema(_ITEMS))
    assert data == {"name": "orchard", "items": [{"n": 1}]}


@pytest.mark.parametrize("text", ["", "orchard", '{"name": ', "```json\n{}\n```"])
def test_parse_structured_refuses_text_that_is_not_json(text):
    with pytest.raises(InvalidOutput) as refused:
        parse_structured(text, check_schema(_ITEMS))
    assert refused.value.message == "the answer is not JSON"
    assert refused.value.code == "invalid_output"
    assert refused.value.status == 502


def test_parse_structured_refuses_a_schema_miss_and_names_the_path():
    text = '{"name": 3, "items": [{"n": 1}, {"n": "two"}]}'
    with pytest.raises(InvalidOutput) as refused:
        parse_structured(text, check_schema(_ITEMS))
    assert refused.value.message == "the answer does not fit the schema"
    assert refused.value.details == {"paths": ["items/1/n", "name"]}


def test_parse_structured_names_at_most_ten_paths():
    schema = {
        "type": "object",
        "properties": {f"p{i:02d}": {"type": "integer"} for i in range(12)},
    }
    text = json.dumps({f"p{i:02d}": "x" for i in range(12)})
    with pytest.raises(InvalidOutput) as refused:
        parse_structured(text, check_schema(schema))
    assert refused.value.details == {"paths": [f"p{i:02d}" for i in range(10)]}


def test_responses_text_takes_message_parts_and_skips_reasoning_items():
    doc = {
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "thinking about the orchard"}],
                "content": [{"type": "output_text", "text": "REASONING"}],
            },
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Lio Varn "},
                    {"type": "refusal", "refusal": "no"},
                    {"type": "output_text", "text": "tends the orchard"},
                ],
            },
            {"type": "function_call", "content": [{"type": "output_text", "text": "CALL"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "."}]},
        ]
    }
    assert responses_text(doc) == "Lio Varn tends the orchard."


@pytest.mark.parametrize("doc", [{}, {"output": None}, {"output": [{"type": "reasoning"}]}])
def test_responses_text_of_a_document_with_no_message_is_empty(doc):
    assert responses_text(doc) == ""


def test_responses_usage_reads_cached_and_reasoning_tokens():
    doc = {
        "usage": {
            "input_tokens": 7043,
            "input_tokens_details": {"cached_tokens": 7025},
            "output_tokens": 55,
            "output_tokens_details": {"reasoning_tokens": 40},
        }
    }
    assert responses_usage(doc) == Usage(7043, 7025, 55, 40)
    assert responses_usage({}) == Usage()
    assert responses_usage({"usage": {"input_tokens": -3, "output_tokens": True}}) == Usage()


def test_chat_usage_reads_cached_and_reasoning_tokens():
    usage = {
        "prompt_tokens": 900,
        "prompt_tokens_details": {"cached_tokens": 512},
        "completion_tokens": 120,
        "completion_tokens_details": {"reasoning_tokens": 80},
    }
    assert chat_usage(usage) == Usage(900, 512, 120, 80)
    assert chat_usage(None) == Usage()
    assert chat_usage({"prompt_tokens": "900", "completion_tokens": 1.5}) == Usage()


def test_default_cache_key_is_stable_per_job_and_prefix():
    prefix = "You read notes about the orchard and return facts."
    key = default_cache_key("extraction", prefix)
    assert key == default_cache_key("extraction", prefix)
    assert key == "grove:extraction:" + hashlib.sha256(f"\0{prefix}".encode()).hexdigest()[:24]
    salted = default_cache_key("extraction", prefix, "world-a")
    assert salted == default_cache_key("extraction", prefix, "world-a")
    assert salted != key and salted != default_cache_key("extraction", prefix, "world-b")
    assert default_cache_key("extraction", prefix + " ") != key
    assert default_cache_key("verification", prefix) != key


@pytest.mark.parametrize("prefix", [None, ""])
def test_default_cache_key_is_none_with_no_prefix(prefix):
    assert default_cache_key("extraction", prefix) is None


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_glimmer_effort_goes_in_the_chat_template(effort):
    assert glimmer_effort_fields(effort) == {"chat_template_kwargs": {"reasoning_strength": effort}}


def test_glimmer_effort_fields_are_empty_with_no_effort():
    assert glimmer_effort_fields(None) == {}


# The budget's units: the ceiling test, the reset time and the error a deferred job gets.


@pytest.mark.parametrize(
    ("budget", "is_reached"),
    [
        (BudgetState(spent=5.0, ceiling=5.0, state="open"), True),
        (BudgetState(spent=5.01, ceiling=5.0, state="open"), True),
        (BudgetState(spent=4.99, ceiling=5.0, state="open"), False),
        (BudgetState(spent=1000.0, ceiling=None, state="open"), False),
        (BudgetState(spent=0.0, ceiling=None, state="reached"), True),
        (BudgetState(spent=0.0, ceiling=0.0, state="open"), True),
    ],
)
def test_reached(budget, is_reached):
    assert reached(budget) is is_reached


def test_the_budget_resets_at_the_next_utc_midnight():
    assert resets_at(date(2026, 11, 2)) == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert resets_at(date(2026, 12, 31)) == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


def test_budget_reached_is_a_retryable_429():
    error = BudgetReached("waits", details={"job": "verification"})
    assert (error.code, error.status, error.retryable) == ("budget_reached", 429, True)
    assert error.as_body() == {
        "error": {
            "code": "budget_reached",
            "type": "budget_reached",
            "message": "waits",
            "details": {"job": "verification"},
            "retryable": True,
        }
    }
    assert (ModelRefused.status, ModelRefused.retryable) == (400, False)
    assert (EgressRefused.code, EgressRefused.status) == ("egress_refused", 403)
