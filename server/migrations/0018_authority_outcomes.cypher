// Inactive authority schema only. No bootstrap, backfill, seed, or runtime activation.
CREATE CONSTRAINT authority_projection_world_unique IF NOT EXISTS FOR (n:AuthorityProjection) REQUIRE n.world_id IS UNIQUE;
CREATE CONSTRAINT authority_operation_id_unique IF NOT EXISTS FOR (n:AuthorityOperation) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT authority_operation_key_unique IF NOT EXISTS FOR (n:AuthorityOperation) REQUIRE (n.world_id, n.idempotency_key) IS UNIQUE;
CREATE CONSTRAINT authority_approval_use_id_unique IF NOT EXISTS FOR (n:AuthorityApprovalUse) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT authority_approval_use_world_unique IF NOT EXISTS FOR (n:AuthorityApprovalUse) REQUIRE (n.world_id, n.approval_id) IS UNIQUE;
