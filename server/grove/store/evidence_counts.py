"""Evidence counts (D2, spec line 766): the earning bars count episodes, never rows.

A ten-month export earns as many episodes as it has windows, and two rows drawn from one message
count once. The bar for a word counts across every link of that word (D8), so one count runs over
all the targets together: a count per target, summed, would count one episode once per target.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING

from grove.store.current import ids_of, read_rows
from grove.store.query import WorldQuery

if TYPE_CHECKING:
    from grove.writer.tx import WorldTx

_DISTINCT_EPISODES = (
    "MATCH (e:Evidence) WHERE e.target_id IN $ids AND e.polarity = 'supports' "
    "AND e.active = true RETURN count(DISTINCT e.episode_id) AS episodes"
)


def distinct_episodes(source: "WorldQuery | WorldTx", target_ids: Iterable[str]) -> int:
    """The distinct episode ids among the active supporting rows of all the targets (items, links
    or beliefs). A WorldTx counts the rows its own transaction wrote."""
    ids = ids_of(target_ids)
    if not ids:
        return 0
    return read_rows(source, _DISTINCT_EPISODES, ids=ids)[0]["episodes"]
