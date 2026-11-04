// One instance per selected read-only source. Upload connectors have no instance_key.
CREATE CONSTRAINT connectors_world_id_provider_instance_key_unique IF NOT EXISTS FOR (n:Connector) REQUIRE (n.world_id, n.provider, n.instance_key) IS UNIQUE;
