// The lexical indexes.

// Substring match on titles and normalised phrases.
CREATE TEXT INDEX items_title_text IF NOT EXISTS FOR (n:Item) ON (n.title);
CREATE TEXT INDEX aliases_normalised_phrase_text IF NOT EXISTS FOR (n:Alias) ON (n.normalised_phrase);

// Full text, with accents and case folded.
CREATE FULLTEXT INDEX grove_items_lexical IF NOT EXISTS FOR (n:Item) ON EACH [n.world_id, n.title, n.summary, n.body, n.property_values, n.search_terms]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}};
CREATE FULLTEXT INDEX grove_aliases_lexical IF NOT EXISTS FOR (n:Alias) ON EACH [n.world_id, n.phrase, n.normalised_phrase]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}};
CREATE FULLTEXT INDEX grove_past_values IF NOT EXISTS FOR (n:PastValue) ON EACH [n.world_id, n.value]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}};
CREATE FULLTEXT INDEX grove_value_index IF NOT EXISTS FOR (n:Item|Alias|HistoryValue|Kind|Meaning|PropertyName|RunRecord) ON EACH [n.world_id, n.title, n.summary, n.body, n.property_values, n.search_terms, n.phrase, n.normalised_phrase, n.value, n.name, n.proposed_operations]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}};
