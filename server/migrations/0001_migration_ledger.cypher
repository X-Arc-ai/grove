// The ledger of applied migrations comes first.

// migrations
CREATE CONSTRAINT migrations_name_unique IF NOT EXISTS FOR (n:Migration) REQUIRE n.name IS UNIQUE;
