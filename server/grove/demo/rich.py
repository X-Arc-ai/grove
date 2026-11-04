"""Add the pinned synthetic corpus to the existing designated world only."""

import argparse
import json

from grove.demo.corpus import manifest, stories
from grove.demo.provision import private_email_hash
from grove.demo_policy import DemoDenied, require_corpus_identity
from grove.store.connection import open_driver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the fixed synthetic demo corpus.")
    parser.add_argument("action", choices=("manifest", "seed", "check", "graph-check"))
    parser.add_argument("--max-stories", type=int, default=40)
    args = parser.parse_args(argv)
    try:
        require_corpus_identity()
        if not 1 <= args.max_stories <= len(stories()):
            raise DemoDenied()
        if args.action == "manifest":
            print(json.dumps(manifest(), sort_keys=True))
            return 0
        # The file is consumed by the existing descriptor-checked reader. Neither
        # its path, contents, hash, nor the account/world id appears in output.
        hashed = private_email_hash()
        from grove.writer.demo_seed import inspect_seed, resolve_target, seed_story

        with open_driver() as driver:
            report = inspect_seed(driver, hashed)
            if args.action == "graph-check":
                from grove import clock
                from grove.store.query import WorldQuery
                from grove.views.graph import Window, graph_payload

                target = resolve_target(driver, hashed)
                payload = graph_payload(
                    WorldQuery(driver, target["world_id"]),
                    WorldQuery.primitives(driver),
                    window=Window(),
                    self_item_id=target["self_item_id"],
                    now=clock.now(target["world_id"]),
                )
                if resolve_target(driver, hashed) != target:
                    raise DemoDenied()
                json.dumps(payload, allow_nan=False)
                nodes = payload["elements"]["nodes"]
                edges = payload["elements"]["edges"]
                ids = {node["data"]["id"] for node in nodes}
                if len(ids) != len(nodes) or any(
                    edge["data"]["source"] not in ids or edge["data"]["target"] not in ids
                    for edge in edges
                ):
                    raise DemoDenied()
                print(
                    json.dumps(
                        {
                            "graph_nodes": len(nodes),
                            "graph_edges": len(edges),
                            "finite": True,
                            "valid_endpoints": True,
                            "truncated": payload["selection"]["truncated"],
                            "stored_items": report["world_items"],
                            "stored_active_links": report["world_active_links"],
                        },
                        sort_keys=True,
                    )
                )
                return 0
            if args.action == "seed":
                first = report["completed_stories"]
                for story in stories()[first : first + args.max_stories]:
                    seed_story(driver, hashed, story.key)
                report = inspect_seed(driver, hashed)
        keys = (
            "completed_stories",
            "items",
            "links",
            "sources",
            "episodes",
            "screening_receipts",
            "saved_requests",
            "world_items",
            "world_active_links",
        )
        safe = {key: report[key] for key in keys if key in report and type(report[key]) is int}
        safe.update(
            state="complete" if report["state"] == "complete" else "partial",
            synthetic=True,
            model_calls=0,
            model_spend=0,
        )
        print(json.dumps(safe, sort_keys=True, allow_nan=False))
        return 0 if safe["state"] == "complete" else 2
    except Exception:
        # Driver exceptions can contain private paths, queries and credentials.
        print("synthetic demo operation refused or unavailable; inspect before retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
