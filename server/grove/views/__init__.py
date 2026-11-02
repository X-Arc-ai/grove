"""The read side of the person's views: the graph, a node, the schema, the health panel.

Pure functions over WorldQuery and PlatformQuery reads. Nothing here writes: the one write of the
views, a map position, goes through grove.writer.positions. docs/views.md has the HTTP contract.
"""
