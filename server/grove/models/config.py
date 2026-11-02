"""The layer's configuration: model ids, the job table, the price table and the settings from env.

Model ids live in jobs.json and prices live in prices.json (S24), never in code. A contributor id
is refused wherever it appears: Meta may train on what reaches it (decision 12).
"""

import json
import math
import os
from dataclasses import dataclass, field
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from grove.models.errors import ModelRefused, NotConfigured
from grove.store.enums import ENUMS

DEFAULT_META_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 18300
DEFAULT_DAILY_BUDGET = 5.0
UNLIMITED = "unlimited"
# Meta counts reasoning tokens against the output limit, and a one-word answer used up to 340 of
# them in our probes, so no call may ask for fewer than this.
MIN_OUTPUT_TOKENS = 1024
DEFAULT_OUTPUT_TOKENS = 8192
AT_CEILING = ("continue", "defer")
ROUTES = ("meta", "modal")
BASES = ("tokens", "audio", "gpu_seconds")
_RATES = {
    "tokens": ("input_per_million", "cached_per_million", "output_per_million"),
    "audio": ("per_hour",),
    "gpu_seconds": ("per_second",),
}
_CONTRIBUTOR = "contributor"
# The egress allowlist comes from these URLs, and the keys go to them, so a URL from the env may
# name only Meta's API host or a Modal server (A.1: the Model API and the judgment host).
META_HOSTS = frozenset({"api.meta.ai"})
GLIMMER_HOST_SUFFIXES = (".modal.direct", ".modal.run")
_EFFORTS = frozenset(ENUMS["effort"])
_MIN_SECRET = 32


def refuse_contributor(model: str) -> None:
    if _CONTRIBUTOR in model.lower():
        raise ModelRefused("a contributor model id is refused: never send data to it")


@dataclass(frozen=True)
class ModelSpec:
    id: str
    family: str
    route: str
    efforts: tuple[str, ...]
    chat: bool


@dataclass(frozen=True)
class Job:
    name: str
    family: str
    effort: str | None
    at_ceiling: str


@dataclass(frozen=True)
class Price:
    basis: str
    input_per_million: float = 0.0
    cached_per_million: float = 0.0
    output_per_million: float = 0.0
    per_hour: float = 0.0
    per_second: float = 0.0


def _read_json(name: str) -> dict[str, Any]:
    return json.loads(resources.files("grove.models").joinpath(name).read_text(encoding="utf-8"))


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    if value < 0:
        raise ValueError(f"{where} must not be negative")
    return float(value)


@dataclass(frozen=True)
class Catalog:
    families: dict[str, str]
    models: dict[str, ModelSpec]
    jobs: dict[str, Job]
    prices: dict[str, Price]

    def model(self, model_id: str) -> ModelSpec:
        refuse_contributor(model_id)
        spec = self.models.get(model_id)
        if spec is None:
            raise ModelRefused("unknown model", details={"known": sorted(self.models)})
        return spec

    def job(self, name: str) -> Job:
        job = self.jobs.get(name)
        if job is None:
            raise ModelRefused("unknown job", details={"known": sorted(self.jobs)})
        return job

    def model_for(self, job: Job) -> ModelSpec:
        return self.model(self.families[job.family])

    def effort_for(self, job: Job, spec: ModelSpec, effort: str | None) -> str | None:
        """Effort is explicit on every call (D15): the caller's, or the job table's."""
        chosen = effort if effort is not None else job.effort
        if not spec.efforts:
            if chosen is not None:
                raise ModelRefused("this model takes no effort")
            return None
        if chosen not in spec.efforts:
            raise ModelRefused("effort not taken by this model", details={"efforts": spec.efforts})
        return chosen

    def cost(
        self,
        model_id: str,
        *,
        input_tokens: int = 0,
        cached_tokens: int = 0,
        output_tokens: int = 0,
        audio_seconds: float = 0.0,
        gpu_seconds: float = 0.0,
    ) -> float:
        """USD from the price table. Input tokens include the cached ones, as Meta reports them."""
        price = self.prices.get(model_id)
        if price is None:
            raise ModelRefused("no price for this model; add it to prices.json")
        if price.basis == "tokens":
            uncached = max(input_tokens - cached_tokens, 0)
            return (
                uncached * price.input_per_million
                + min(cached_tokens, input_tokens) * price.cached_per_million
                + output_tokens * price.output_per_million
            ) / 1_000_000
        if price.basis == "audio":
            return audio_seconds / 3600 * price.per_hour
        return gpu_seconds * price.per_second


def load_catalog() -> Catalog:
    jobs_doc = _read_json("jobs.json")
    prices_doc = _read_json("prices.json")
    models: dict[str, ModelSpec] = {}
    for model_id, row in jobs_doc["models"].items():
        refuse_contributor(model_id)
        if row["route"] not in ROUTES:
            raise ValueError(f"model {model_id}: unknown route")
        efforts = tuple(row["efforts"])
        if not set(efforts) <= _EFFORTS:
            raise ValueError(f"model {model_id}: an effort outside the effort enum")
        models[model_id] = ModelSpec(model_id, row["family"], row["route"], efforts, row["chat"])
    families = dict(jobs_doc["families"])
    for family, model_id in families.items():
        refuse_contributor(model_id)
        if model_id not in models or models[model_id].family != family:
            raise ValueError(f"family {family}: its model is not declared under that family")
    jobs: dict[str, Job] = {}
    for name, row in jobs_doc["jobs"].items():
        if row["family"] not in families:
            raise ValueError(f"job {name}: unknown family")
        if row["at_ceiling"] not in AT_CEILING:
            raise ValueError(f"job {name}: at_ceiling must be one of {AT_CEILING}")
        spec = models[families[row["family"]]]
        effort = row["effort"]
        fits = effort in spec.efforts if spec.efforts else effort is None
        if not fits:
            raise ValueError(f"job {name}: its effort does not fit its model")
        jobs[name] = Job(name, row["family"], effort, row["at_ceiling"])
    prices: dict[str, Price] = {}
    for model_id, row in prices_doc["models"].items():
        refuse_contributor(model_id)
        if row["basis"] not in BASES:
            raise ValueError(f"price {model_id}: unknown basis")
        numbers = {
            key: _number(value, f"price {model_id} {key}")
            for key, value in row.items()
            if key.endswith(("_per_million", "per_hour", "per_second"))
        }
        missing = set(_RATES[row["basis"]]) - set(numbers)
        if missing:
            # A missing rate would meter that part of a call as free.
            raise ValueError(f"price {model_id}: missing {sorted(missing)}")
        prices[model_id] = Price(basis=row["basis"], **numbers)
    unpriced = set(models) - set(prices)
    if unpriced:
        raise ValueError(f"models with no price: {sorted(unpriced)}")
    return Catalog(families, models, jobs, prices)


def _https_base(name: str, value: str, allowed) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise NotConfigured(f"{name} must be an https URL with a host and no user part")
    try:
        port = parts.port
    except ValueError:
        port = -1
    if port not in (None, 443) or not allowed(parts.hostname.rstrip(".").lower()):
        raise NotConfigured(f"{name} names a host outside the model hosts")
    return value.rstrip("/")


def _meta_host(host: str) -> bool:
    return host in META_HOSTS


def _glimmer_host(host: str) -> bool:
    return host.endswith(GLIMMER_HOST_SUFFIXES) and len(host) > max(map(len, GLIMMER_HOST_SUFFIXES))


def _budget(value: str | None) -> float | None:
    if value is None or value == "":
        return DEFAULT_DAILY_BUDGET
    if value == UNLIMITED:
        return None
    try:
        return _number(float(value), "GROVE_DAILY_BUDGET_USD")
    except ValueError as exc:
        raise NotConfigured("GROVE_DAILY_BUDGET_USD must be a number or 'unlimited'") from exc


@dataclass(frozen=True)
class ModelSettings:
    meta_base_url: str = DEFAULT_META_BASE_URL
    meta_key: str | None = field(default=None, repr=False)
    glimmer_base_url: str | None = None
    glimmer_key: str | None = field(default=None, repr=False)
    daily_budget: float | None = DEFAULT_DAILY_BUDGET
    gateway_secret: bytes | None = field(default=None, repr=False)
    gateway_host: str = DEFAULT_GATEWAY_HOST
    gateway_port: int = DEFAULT_GATEWAY_PORT

    def require_meta_key(self) -> str:
        if not self.meta_key:
            raise NotConfigured("MODEL_API_KEY is not set")
        return self.meta_key

    def require_glimmer(self) -> tuple[str, str]:
        if not self.glimmer_base_url:
            raise NotConfigured("GROVE_GLIMMER_BASE_URL is not set")
        if not self.glimmer_key:
            raise NotConfigured("GROVE_GLIMMER_KEY is not set")
        return self.glimmer_base_url, self.glimmer_key

    def require_gateway_secret(self) -> bytes:
        if self.gateway_secret is None or len(self.gateway_secret) < _MIN_SECRET:
            raise NotConfigured(f"GROVE_GATEWAY_SECRET must hold at least {_MIN_SECRET} bytes")
        return self.gateway_secret

    def model_hosts(self) -> frozenset[str]:
        """The hosts the model-call layer may reach: the egress allowlist's model entries."""
        hosts = {urlsplit(self.meta_base_url).hostname}
        if self.glimmer_base_url:
            hosts.add(urlsplit(self.glimmer_base_url).hostname)
        return frozenset(host for host in hosts if host)


def load_model_settings(env: dict[str, str] | None = None) -> ModelSettings:
    env = os.environ if env is None else env
    glimmer = env.get("GROVE_GLIMMER_BASE_URL") or None
    secret = env.get("GROVE_GATEWAY_SECRET")
    port = env.get("GROVE_GATEWAY_PORT")
    return ModelSettings(
        meta_base_url=_https_base(
            "GROVE_META_BASE_URL",
            env.get("GROVE_META_BASE_URL") or DEFAULT_META_BASE_URL,
            _meta_host,
        ),
        meta_key=env.get("MODEL_API_KEY") or None,
        glimmer_base_url=(
            _https_base("GROVE_GLIMMER_BASE_URL", glimmer, _glimmer_host) if glimmer else None
        ),
        glimmer_key=env.get("GROVE_GLIMMER_KEY") or None,
        daily_budget=_budget(env.get("GROVE_DAILY_BUDGET_USD")),
        gateway_secret=secret.encode("utf-8") if secret else None,
        gateway_host=env.get("GROVE_GATEWAY_HOST") or DEFAULT_GATEWAY_HOST,
        gateway_port=_port(port) if port else DEFAULT_GATEWAY_PORT,
    )


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise NotConfigured("GROVE_GATEWAY_PORT must be a port number")
    return port
