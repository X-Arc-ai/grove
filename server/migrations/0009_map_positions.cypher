// Rows added by later lanes: map positions per item (views-a).

// map_positions
CREATE CONSTRAINT map_positions_id_unique IF NOT EXISTS FOR (n:MapPosition) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT map_positions_world_id_item_id_unique IF NOT EXISTS FOR (n:MapPosition) REQUIRE (n.world_id, n.item_id) IS UNIQUE;
