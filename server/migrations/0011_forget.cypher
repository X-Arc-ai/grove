CREATE CONSTRAINT forget_approvals_id_unique IF NOT EXISTS FOR (n:ForgetApproval) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT forget_approvals_token_hash_unique IF NOT EXISTS FOR (n:ForgetApproval) REQUIRE n.token_hash IS UNIQUE;
CREATE RANGE INDEX forget_approvals_world_id_idx IF NOT EXISTS FOR (n:ForgetApproval) ON (n.world_id);
CREATE FULLTEXT INDEX grove_forget_values IF NOT EXISTS FOR (n:Item|Alias|HistoryValue|Kind|Meaning|PropertyName|RunRecord|Source) ON EACH [n.world_id, n.title, n.summary, n.body, n.properties, n.property_values, n.search_terms, n.status, n.status_vocabulary, n.phrase, n.normalised_phrase, n.value, n.name, n.proposed_operations, n.content] OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}};
