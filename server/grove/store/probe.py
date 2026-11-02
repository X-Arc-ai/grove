"""Is the store there? One read with no retry, so a health check answers fast when it is not."""

from neo4j import READ_ACCESS, Driver
from neo4j.exceptions import DriverError, Neo4jError

from grove.store.connection import DATABASE

_PING = "CYPHER 25 RETURN 1 AS ok"


def store_reachable(driver: Driver) -> bool:
    # An auto-commit run is not retried, unlike execute_read, which retries for up to 30 s.
    try:
        with driver.session(database=DATABASE, default_access_mode=READ_ACCESS) as session:
            record = session.run(_PING).single()
    except (DriverError, Neo4jError, OSError):
        return False
    return record is not None and record["ok"] == 1
