"""The egress allowlist for agent runtimes and the model-call layer (A.1, D14).

Every outbound request of the layer goes through AllowlistTransport, which checks the URL before
any socket opens. So a redirect cannot leave the list either: httpx sends each hop through the
transport again.

The runtimes: the layer itself, and the four agents. Each agent reaches a model only through the
internal gateway, which calls out with the layer's runtime. The clean-up agent alone may also
reach the read-only connectors, which the `cleanup` lane adds through `connector_hosts`.

Out of reach of this module: a process that opens its own socket. On the box, the runner's
network rules stop that; this module covers what Grove's own code sends.
"""

import ipaddress
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from grove.models.config import ModelSettings
from grove.models.errors import EgressRefused

MODEL_LAYER = "model_layer"
AGENTS = ("grove", "learner", "curator", "connector")
RUNTIMES = (MODEL_LAYER, *AGENTS)
CLEANUP_AGENT = "curator"
_HTTPS_PORT = 443


def _host(value: str) -> str:
    return value.strip().rstrip(".").lower()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class EgressPolicy:
    def __init__(self, allow: Mapping[str, Iterable[str]]) -> None:
        unknown = set(allow) - set(RUNTIMES)
        if unknown:
            raise ValueError(f"unknown runtimes: {sorted(unknown)}")
        self._allow = {runtime: frozenset(map(_host, hosts)) for runtime, hosts in allow.items()}

    @classmethod
    def for_settings(
        cls, settings: ModelSettings, *, connector_hosts: Iterable[str] = ()
    ) -> "EgressPolicy":
        models = settings.model_hosts()
        allow = {runtime: models for runtime in RUNTIMES}
        allow[CLEANUP_AGENT] = models | frozenset(connector_hosts)
        return cls(allow)

    def hosts(self, runtime: str) -> frozenset[str]:
        if runtime not in self._allow:
            raise EgressRefused("unknown runtime", details={"runtime": runtime})
        return self._allow[runtime]

    def check(self, runtime: str, url: str) -> None:
        """Raise EgressRefused unless `url` is https to a listed host on the default port."""
        allowed = self.hosts(runtime)
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = -1
        host = _host(parts.hostname or "")
        if parts.scheme != "https":
            raise EgressRefused("only https leaves the box", details={"runtime": runtime})
        if parts.username is not None or parts.password is not None:
            raise EgressRefused("a URL with a user part is refused", details={"runtime": runtime})
        if port not in (None, _HTTPS_PORT):
            raise EgressRefused("only the https port is allowed", details={"runtime": runtime})
        if not host or host not in allowed:
            raise EgressRefused(
                "host not on the egress allowlist",
                details={"runtime": runtime, "host": host, "ip_literal": _is_ip(host)},
            )


class AllowlistTransport(httpx.BaseTransport):
    def __init__(
        self, policy: EgressPolicy, runtime: str, inner: httpx.BaseTransport | None = None
    ) -> None:
        policy.hosts(runtime)
        self._policy = policy
        self._runtime = runtime
        self._inner = inner or httpx.HTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._policy.check(self._runtime, str(request.url))
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class AsyncAllowlistTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, policy: EgressPolicy, runtime: str, inner: httpx.AsyncBaseTransport | None = None
    ) -> None:
        policy.hosts(runtime)
        self._policy = policy
        self._runtime = runtime
        self._inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._policy.check(self._runtime, str(request.url))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
