// Durable material metadata. Content remains in screened Sources.
CREATE CONSTRAINT material_revisions_id_unique IF NOT EXISTS FOR (n:MaterialRevision) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT material_revisions_world_id_material_id_revision_unique IF NOT EXISTS FOR (n:MaterialRevision) REQUIRE (n.world_id, n.material_id, n.revision) IS UNIQUE;
CREATE CONSTRAINT material_revisions_world_id_request_key_hash_unique IF NOT EXISTS FOR (n:MaterialRevision) REQUIRE (n.world_id, n.request_key_hash) IS UNIQUE;
CREATE INDEX material_revisions_world_id_state_kind_created_at_idx IF NOT EXISTS FOR (n:MaterialRevision) ON (n.world_id, n.state, n.kind, n.created_at);
CREATE INDEX material_revisions_world_id_material_id_idx IF NOT EXISTS FOR (n:MaterialRevision) ON (n.world_id, n.material_id);
