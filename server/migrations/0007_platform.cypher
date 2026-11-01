// Platform rows beyond B.1.

// worlds
CREATE CONSTRAINT worlds_id_unique IF NOT EXISTS FOR (n:World) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX worlds_owner_user_id_idx IF NOT EXISTS FOR (n:World) ON (n.owner_user_id);

// users
CREATE CONSTRAINT users_id_unique IF NOT EXISTS FOR (n:User) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT users_email_hash_unique IF NOT EXISTS FOR (n:User) REQUIRE n.email_hash IS UNIQUE;

// sessions
CREATE CONSTRAINT sessions_id_unique IF NOT EXISTS FOR (n:Session) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT sessions_token_hash_unique IF NOT EXISTS FOR (n:Session) REQUIRE n.token_hash IS UNIQUE;
CREATE RANGE INDEX sessions_user_id_idx IF NOT EXISTS FOR (n:Session) ON (n.user_id);

// oauth_tokens
CREATE CONSTRAINT oauth_tokens_id_unique IF NOT EXISTS FOR (n:OAuthToken) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT oauth_tokens_token_hash_unique IF NOT EXISTS FOR (n:OAuthToken) REQUIRE n.token_hash IS UNIQUE;
CREATE RANGE INDEX oauth_tokens_client_id_idx IF NOT EXISTS FOR (n:OAuthToken) ON (n.client_id);
CREATE RANGE INDEX oauth_tokens_user_id_idx IF NOT EXISTS FOR (n:OAuthToken) ON (n.user_id);

// authorization_codes
CREATE CONSTRAINT authorization_codes_id_unique IF NOT EXISTS FOR (n:AuthCode) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT authorization_codes_code_hash_unique IF NOT EXISTS FOR (n:AuthCode) REQUIRE n.code_hash IS UNIQUE;

// email_codes
CREATE CONSTRAINT email_codes_id_unique IF NOT EXISTS FOR (n:EmailCode) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX email_codes_email_hash_idx IF NOT EXISTS FOR (n:EmailCode) ON (n.email_hash);

// upload_tokens
CREATE CONSTRAINT upload_tokens_id_unique IF NOT EXISTS FOR (n:UploadToken) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT upload_tokens_token_hash_unique IF NOT EXISTS FOR (n:UploadToken) REQUIRE n.token_hash IS UNIQUE;
