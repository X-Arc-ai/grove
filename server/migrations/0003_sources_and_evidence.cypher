// B.1: sources and evidence.

// connectors
CREATE CONSTRAINT connectors_id_unique IF NOT EXISTS FOR (n:Connector) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX connectors_world_id_type_idx IF NOT EXISTS FOR (n:Connector) ON (n.world_id, n.type);

// capabilities
CREATE CONSTRAINT capabilities_id_unique IF NOT EXISTS FOR (n:Capability) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX capabilities_connector_type_registry_id_idx IF NOT EXISTS FOR (n:Capability) ON (n.connector_type, n.registry_id);
CREATE RANGE INDEX capabilities_connector_type_selector_idx IF NOT EXISTS FOR (n:Capability) ON (n.connector_type, n.selector);

// sources
CREATE CONSTRAINT sources_id_unique IF NOT EXISTS FOR (n:Source) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX sources_world_id_connector_id_external_locator_idx IF NOT EXISTS FOR (n:Source) ON (n.world_id, n.connector_id, n.external_locator);

// episodes
CREATE CONSTRAINT episodes_id_unique IF NOT EXISTS FOR (n:Episode) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT episodes_world_id_connector_id_first_message_key_unique IF NOT EXISTS FOR (n:Episode) REQUIRE (n.world_id, n.connector_id, n.first_message_key) IS UNIQUE;

// evidence
CREATE CONSTRAINT evidence_id_unique IF NOT EXISTS FOR (n:Evidence) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT evidence_world_id_message_key_target_id_polarity_unique IF NOT EXISTS FOR (n:Evidence) REQUIRE (n.world_id, n.message_key, n.target_id, n.polarity) IS UNIQUE;
CREATE RANGE INDEX evidence_world_id_target_type_target_id_polarity_active_idx IF NOT EXISTS FOR (n:Evidence) ON (n.world_id, n.target_type, n.target_id, n.polarity, n.active);
CREATE RANGE INDEX evidence_episode_id_idx IF NOT EXISTS FOR (n:Evidence) ON (n.episode_id);
CREATE RANGE INDEX evidence_source_id_locator_idx IF NOT EXISTS FOR (n:Evidence) ON (n.source_id, n.locator);

// holds
CREATE CONSTRAINT holds_id_unique IF NOT EXISTS FOR (n:Hold) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX holds_world_id_state_expires_at_idx IF NOT EXISTS FOR (n:Hold) ON (n.world_id, n.state, n.expires_at);

// unresolved_mentions
CREATE CONSTRAINT unresolved_mentions_id_unique IF NOT EXISTS FOR (n:UnresolvedMention) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX unresolved_mentions_world_id_state_expires_at_idx IF NOT EXISTS FOR (n:UnresolvedMention) ON (n.world_id, n.state, n.expires_at);

// screening_receipts
CREATE CONSTRAINT screening_receipts_world_id_source_id_unique IF NOT EXISTS FOR (n:ScreeningReceipt) REQUIRE (n.world_id, n.source_id) IS UNIQUE;
