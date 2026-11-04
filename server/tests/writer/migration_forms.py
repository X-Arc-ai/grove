"""The one bounded data migration accepted by the schema inventory test."""

_GENERATION_INITIALIZER = (
    "MATCH (w:World) WHERE w.read_generation IS NULL SET w.read_generation = 0"
)


def is_generation_initializer(statement: str) -> bool:
    return statement == _GENERATION_INITIALIZER
