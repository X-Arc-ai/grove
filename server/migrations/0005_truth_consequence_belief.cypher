// B.1: truth, consequence and belief.

// verifications
CREATE CONSTRAINT verifications_id_unique IF NOT EXISTS FOR (n:Verification) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX verifications_world_id_claim_id_at_idx IF NOT EXISTS FOR (n:Verification) ON (n.world_id, n.claim_id, n.at);
CREATE RANGE INDEX verifications_run_id_idx IF NOT EXISTS FOR (n:Verification) ON (n.run_id);

// horizon_stats
CREATE CONSTRAINT horizon_stats_world_id_meaning_or_property_name_unique IF NOT EXISTS FOR (n:HorizonStat) REQUIRE (n.world_id, n.meaning_or_property_name) IS UNIQUE;

// impacts
CREATE CONSTRAINT impacts_link_id_unique IF NOT EXISTS FOR (n:Impact) REQUIRE n.link_id IS UNIQUE;
CREATE CONSTRAINT impacts_world_id_trigger_change_set_affected_item_unique IF NOT EXISTS FOR (n:Impact) REQUIRE (n.world_id, n.trigger_change_set, n.affected_item) IS UNIQUE;

// cascade_proposals
CREATE CONSTRAINT cascade_proposals_id_unique IF NOT EXISTS FOR (n:CascadeProposal) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX cascade_proposals_world_id_state_idx IF NOT EXISTS FOR (n:CascadeProposal) ON (n.world_id, n.state);

// beliefs
CREATE CONSTRAINT beliefs_item_id_unique IF NOT EXISTS FOR (n:Belief) REQUIRE n.item_id IS UNIQUE;

// contradictions
CREATE CONSTRAINT contradictions_id_unique IF NOT EXISTS FOR (n:Contradiction) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX contradictions_world_id_status_detected_at_idx IF NOT EXISTS FOR (n:Contradiction) ON (n.world_id, n.status, n.detected_at);

// support_changed
CREATE CONSTRAINT support_changed_world_id_belief_id_unique IF NOT EXISTS FOR (n:SupportChanged) REQUIRE (n.world_id, n.belief_id) IS UNIQUE;

// questions
CREATE CONSTRAINT questions_id_unique IF NOT EXISTS FOR (n:Question) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX questions_world_id_state_priority_idx IF NOT EXISTS FOR (n:Question) ON (n.world_id, n.state, n.priority);

// area_audits
CREATE CONSTRAINT area_audits_world_id_run_id_unique IF NOT EXISTS FOR (n:AreaAudit) REQUIRE (n.world_id, n.run_id) IS UNIQUE;
CREATE RANGE INDEX area_audits_area_id_idx IF NOT EXISTS FOR (n:AreaAudit) ON (n.area_id);

// walk_rotation
CREATE CONSTRAINT walk_rotation_world_id_area_id_unique IF NOT EXISTS FOR (n:WalkRotation) REQUIRE (n.world_id, n.area_id) IS UNIQUE;
