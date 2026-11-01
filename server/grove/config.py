"""Settings from the environment. Values never reach a log: the password has no repr."""

import os
from dataclasses import dataclass, field

DEFAULT_NEO4J_URI = "bolt://127.0.0.1:57687"


class ConfigError(RuntimeError):
    """A setting is missing or malformed. The message names the variable, never its value."""


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str
    neo4j_user: str | None = None
    neo4j_password: str | None = field(default=None, repr=False)
    env: str = "development"

    @property
    def neo4j_auth(self) -> tuple[str, str] | None:
        if self.neo4j_user is None or self.neo4j_password is None:
            return None
        return (self.neo4j_user, self.neo4j_password)


def _parse_auth(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        raise ConfigError("NEO4J_AUTH is not set")
    if value == "none":
        return (None, None)
    user, separator, password = value.partition("/")
    if not (user and separator and password):
        raise ConfigError("NEO4J_AUTH must be user/password or none")
    return (user, password)


def load_settings() -> Settings:
    user, password = _parse_auth(os.environ.get("NEO4J_AUTH"))
    return Settings(
        neo4j_uri=os.environ.get("GROVE_NEO4J_URI", DEFAULT_NEO4J_URI),
        neo4j_user=user,
        neo4j_password=password,
        env=os.environ.get("GROVE_ENV", "development"),
    )
