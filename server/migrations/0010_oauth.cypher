// The OAuth rows of hosts-a: a person's consent per host and world (S41), and token families.

// oauth_grants
CREATE CONSTRAINT oauth_grants_id_unique IF NOT EXISTS FOR (n:OAuthGrant) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT oauth_grants_client_id_world_id_unique IF NOT EXISTS FOR (n:OAuthGrant) REQUIRE (n.client_id, n.world_id) IS UNIQUE;
CREATE RANGE INDEX oauth_grants_world_id_idx IF NOT EXISTS FOR (n:OAuthGrant) ON (n.world_id);

// oauth_tokens
CREATE RANGE INDEX oauth_tokens_family_id_idx IF NOT EXISTS FOR (n:OAuthToken) ON (n.family_id);
