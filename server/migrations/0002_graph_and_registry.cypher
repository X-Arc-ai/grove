// B.1: the graph and its registry.

// items
CREATE CONSTRAINT items_id_unique IF NOT EXISTS FOR (n:Item) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX items_world_id_kind_status_role_idx IF NOT EXISTS FOR (n:Item) ON (n.world_id, n.kind, n.status_role);
CREATE RANGE INDEX items_world_id_became_true_earliest_idx IF NOT EXISTS FOR (n:Item) ON (n.world_id, n.became_true_earliest);
CREATE RANGE INDEX items_world_id_became_true_latest_idx IF NOT EXISTS FOR (n:Item) ON (n.world_id, n.became_true_latest);
CREATE RANGE INDEX items_world_id_recorded_idx IF NOT EXISTS FOR (n:Item) ON (n.world_id, n.recorded);

// links
CREATE CONSTRAINT links_id_unique IF NOT EXISTS FOR ()-[r:LINK]-() REQUIRE r.id IS UNIQUE;
CREATE CONSTRAINT links_active_key_unique IF NOT EXISTS FOR ()-[r:LINK]-() REQUIRE r.active_key IS UNIQUE;
CREATE RANGE INDEX links_world_id_from_item_meaning_idx IF NOT EXISTS FOR ()-[r:LINK]-() ON (r.world_id, r.from_item, r.meaning);
CREATE RANGE INDEX links_world_id_to_item_meaning_idx IF NOT EXISTS FOR ()-[r:LINK]-() ON (r.world_id, r.to_item, r.meaning);

// kinds
CREATE CONSTRAINT kinds_id_unique IF NOT EXISTS FOR (n:Kind) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT kinds_world_id_name_unique IF NOT EXISTS FOR (n:Kind) REQUIRE (n.world_id, n.name) IS UNIQUE;

// meanings
CREATE CONSTRAINT meanings_id_unique IF NOT EXISTS FOR (n:Meaning) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT meanings_world_id_name_unique IF NOT EXISTS FOR (n:Meaning) REQUIRE (n.world_id, n.name) IS UNIQUE;

// property_names
CREATE CONSTRAINT property_names_id_unique IF NOT EXISTS FOR (n:PropertyName) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT property_names_world_id_kind_name_unique IF NOT EXISTS FOR (n:PropertyName) REQUIRE (n.world_id, n.kind, n.name) IS UNIQUE;

// aliases
CREATE CONSTRAINT aliases_id_unique IF NOT EXISTS FOR (n:Alias) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT aliases_world_id_normalised_phrase_target_type_target_id_unique IF NOT EXISTS FOR (n:Alias) REQUIRE (n.world_id, n.normalised_phrase, n.target_type, n.target_id) IS UNIQUE;

// ontology_change_log
CREATE CONSTRAINT ontology_change_log_id_unique IF NOT EXISTS FOR (n:OntologyChange) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX ontology_change_log_world_id_at_idx IF NOT EXISTS FOR (n:OntologyChange) ON (n.world_id, n.at);

// distinct_pairs
CREATE CONSTRAINT distinct_pairs_world_id_item_a_item_b_unique IF NOT EXISTS FOR (n:DistinctPair) REQUIRE (n.world_id, n.item_a, n.item_b) IS UNIQUE;

// areas
CREATE CONSTRAINT areas_id_unique IF NOT EXISTS FOR (n:Area) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX areas_world_id_hub_item_idx IF NOT EXISTS FOR (n:Area) ON (n.world_id, n.hub_item);
CREATE RANGE INDEX areas_world_id_dirt_score_idx IF NOT EXISTS FOR (n:Area) ON (n.world_id, n.dirt_score);
CREATE RANGE INDEX areas_world_id_last_audited_at_idx IF NOT EXISTS FOR (n:Area) ON (n.world_id, n.last_audited_at);
