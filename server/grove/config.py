"""Settings from the environment. Values never reach a log: the password has no repr."""

import codecs
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NEO4J_URI = "bolt://127.0.0.1:57687"

RENAME_MAP_VAR = "GROVE_RENAME_MAP"
DENYLIST_VAR = "GROVE_DENYLIST"


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


@dataclass(frozen=True)
class ScreeningSettings:
    """Where the rename map and the denylist live. None means the variable is unset: empty.

    Only outside production: with GROVE_ENV=production an unset variable is a ConfigError.

    The paths have no repr: a home folder path can hold a name that R1 keeps out of logs.
    """

    rename_map: Path | None = field(default=None, repr=False)
    denylist: Path | None = field(default=None, repr=False)


def _optional_path(variable: str) -> Path | None:
    value = os.environ.get(variable)
    if value is None:
        # On the box an unset variable would screen real text with an empty list and no error, and
        # real names would reach the sources table and the model. So production refuses it.
        if os.environ.get("GROVE_ENV") == "production":
            raise ConfigError(f"{variable} is not set, and production needs it")
        return None
    # An empty value is a deploy mistake, not a choice: running without the list would let its
    # names through, so it fails closed.
    if not value.strip():
        raise ConfigError(f"{variable} is set but empty")
    path = _expanded(value)
    if path is None:
        raise ConfigError(f"{variable}: the path cannot be expanded")
    return path


def _expanded(value: str) -> Path | None:
    """The path with its ~ read, or None for an unknown ~user. It returns None instead of
    raising, so the error about a name in the path never travels as the context of a ConfigError."""
    try:
        return Path(value).expanduser()
    except RuntimeError:
        return None


def load_screening_settings() -> ScreeningSettings:
    """Reads the environment only. read_private_file checks each file when it is loaded."""
    return ScreeningSettings(
        rename_map=_optional_path(RENAME_MAP_VAR), denylist=_optional_path(DENYLIST_VAR)
    )


def _open_private(path: Path) -> tuple[bytes | None, str | None]:
    """The bytes of the file, or why they cannot be read.

    It returns the reason instead of raising, so the OS error, whose text holds the path, never
    travels as the context of a ConfigError.
    """
    try:
        # O_NONBLOCK: a FIFO opens at once and is then refused, instead of hanging the loader.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, "the file does not exist"
    except PermissionError:
        return None, "the file cannot be read"
    except OSError:
        return None, "the file cannot be opened"
    # fstat on the open descriptor, so the checked file is the file that is read. It runs before
    # fdopen, which refuses a directory with an error of its own.
    mode = os.fstat(descriptor).st_mode
    if not stat.S_ISREG(mode):
        os.close(descriptor)
        return None, "not a regular file"
    if mode & 0o077:
        os.close(descriptor)
        return None, "the file is open to group or other; chmod 600 it"
    with os.fdopen(descriptor, "rb") as handle:
        try:
            return handle.read(), None
        except OSError:
            return None, "the file cannot be read"


def _bad_utf8_line(data: bytes) -> int | None:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        return data.count(b"\n", 0, error.start) + 1
    return None


def read_private_file(path: Path, variable: str) -> str:
    """The UTF-8 text of a configuration file that only its owner may read.

    Every error names `variable` and never the path or the content: the path can hold a name and
    the content is the list itself.
    """
    data, reason = _open_private(path)
    if data is None:
        raise ConfigError(f"{variable}: {reason}")
    # A byte order mark would otherwise glue itself to the first term, which would never match.
    data = data.removeprefix(codecs.BOM_UTF8)
    line = _bad_utf8_line(data)
    if line is not None:
        raise ConfigError(f"{variable}: line {line} is not UTF-8")
    return data.decode("utf-8")
