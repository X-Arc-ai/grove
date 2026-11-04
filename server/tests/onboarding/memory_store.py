"""Strict in-memory driver for focused reminder tests; no database or real account."""

from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from types import SimpleNamespace

from grove.demo_policy import DESIGNATION_QUERY
from grove.store.query import CYPHER_PREFIX
from grove.views.onboarding import READ_STATE
from grove.views.session import _FIND, token_hash
from grove.writer.onboarding import _SAVE
from grove.writer.tx import _ADVANCE_GENERATION, _LOCK_WORLD, WorldTx

WORLD_A = "t_reminders_11111111-1111-4111-8111-111111111111"
WORLD_B = "t_reminders_22222222-2222-4222-8222-222222222222"


class Result:
    def __init__(self, rows=(), changed=False):
        self.rows = list(rows)
        self.summary = SimpleNamespace(counters=SimpleNamespace(contains_updates=changed))

    def __iter__(self):
        return iter(self.rows)

    def single(self):
        return self.rows[0] if self.rows else None

    def consume(self):
        return self.summary


class Transaction:
    def __init__(self, driver, write=False):
        self.driver = driver
        self.write = write
        self.locked = False
        self.before = None

    def run(self, query, parameters=None, **kwargs):
        args = {**(parameters or {}), **kwargs}
        self.driver.queries.append((self.write, query))
        world = self.driver.worlds.get(args.get("world_id"))
        if query == _LOCK_WORLD:
            assert self.write
            self.driver.lock.acquire()
            self.locked = True
            self.before = deepcopy(self.driver.worlds)
            return Result([{"id": args["world_id"], "generation": world["read_generation"]}])
        if self.write:
            assert self.locked, "World metadata was read before the mandatory World lock"
        if query == CYPHER_PREFIX + _FIND:
            owner = self.driver.sessions.get(args["token_hash"])
            if not owner or owner.get("revoked") or owner["expires"] <= args["now"]:
                return Result()
            owned = sorted(
                (wid, row)
                for wid, row in self.driver.worlds.items()
                if row["account"] == owner["account"]
            )
            return (
                Result(
                    [
                        dict(
                            user_id=owner["account"],
                            world_id=owned[0][0],
                            self_item_id=None,
                            method="email",
                        )
                    ]
                )
                if owned
                else Result()
            )
        if query == CYPHER_PREFIX + READ_STATE:
            return (
                Result(
                    [
                        dict(
                            account=world["account"],
                            requests=world.get("requests"),
                            revision=world.get("revision"),
                        )
                    ]
                )
                if world
                else Result()
            )
        if query == CYPHER_PREFIX + DESIGNATION_QUERY:
            return (
                Result(
                    [
                        dict(
                            user_id=world["account"],
                            demo_version=world.get("demo_version"),
                            demo_world_id=world.get("demo_world_id"),
                            designated_user_id=world.get("designated_user_id"),
                        )
                    ]
                )
                if world
                else Result()
            )
        if query == CYPHER_PREFIX + _SAVE:
            assert self.write
            world.update(requests=args["requests"], revision=args["revision"])
            return Result(changed=True)
        if query == _ADVANCE_GENERATION:
            assert self.write
            world["read_generation"] += 1
            return Result([dict(generation=world["read_generation"])], changed=True)
        raise AssertionError("Unexpected database statement")

    def close(self, failed):
        if self.locked:
            if failed:
                self.driver.worlds = self.before
            self.driver.lock.release()


class Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute_read(self, work, *args, **kwargs):
        with self.driver.lock:
            return work(Transaction(self.driver), *args, **kwargs)


class MemoryDriver:
    def __init__(self):
        self.lock = RLock()
        self.queries = []
        self.worlds = {
            WORLD_A: dict(account="owner-a", read_generation=0),
            WORLD_B: dict(account="owner-b", read_generation=0),
        }
        self.sessions = {
            token_hash("synthetic-a"): dict(
                account="owner-a", expires=datetime(2100, 1, 1, tzinfo=UTC)
            ),
            token_hash("synthetic-b"): dict(
                account="owner-b", expires=datetime(2100, 1, 1, tzinfo=UTC)
            ),
        }

    def session(self, **_):
        return Session(self)

    def transaction(self, work, world_id):
        """Test replacement for the driver's transaction boundary, with real WorldTx.

        This tests reminder callbacks and mutation accounting. It does not establish
        native database locking or isolation; those remain in the writer's tests.
        """
        native = Transaction(self, write=True)
        scoped = WorldTx(native, world_id)
        try:
            scoped._acquire_world_lock()
            result = scoped._finish(work(scoped))
        except BaseException:
            native.close(True)
            raise
        native.close(False)
        return result
