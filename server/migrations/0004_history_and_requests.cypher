// B.1: history and requests.

// change_entries
CREATE CONSTRAINT change_entries_world_id_sequence_unique IF NOT EXISTS FOR (n:ChangeEntry) REQUIRE (n.world_id, n.sequence) IS UNIQUE;
CREATE RANGE INDEX change_entries_world_id_entity_type_entity_id_sequence_idx IF NOT EXISTS FOR (n:ChangeEntry) ON (n.world_id, n.entity_type, n.entity_id, n.sequence);
CREATE RANGE INDEX change_entries_change_set_id_idx IF NOT EXISTS FOR (n:ChangeEntry) ON (n.change_set_id);
CREATE RANGE INDEX change_entries_request_id_idx IF NOT EXISTS FOR (n:ChangeEntry) ON (n.request_id);
CREATE RANGE INDEX change_entries_run_id_idx IF NOT EXISTS FOR (n:ChangeEntry) ON (n.run_id);

// history_values
CREATE CONSTRAINT history_values_id_unique IF NOT EXISTS FOR (n:HistoryValue) REQUIRE n.id IS UNIQUE;

// requests
CREATE CONSTRAINT requests_world_id_id_unique IF NOT EXISTS FOR (n:Request) REQUIRE (n.world_id, n.id) IS UNIQUE;
CREATE RANGE INDEX requests_world_id_status_idx IF NOT EXISTS FOR (n:Request) ON (n.world_id, n.status);

// declines
CREATE CONSTRAINT declines_world_id_request_id_op_id_unique IF NOT EXISTS FOR (n:Decline) REQUIRE (n.world_id, n.request_id, n.op_id) IS UNIQUE;

// jobs
CREATE CONSTRAINT jobs_id_unique IF NOT EXISTS FOR (n:Job) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX jobs_world_id_lane_state_priority_run_after_idx IF NOT EXISTS FOR (n:Job) ON (n.world_id, n.lane, n.state, n.priority, n.run_after);

// run_records
CREATE CONSTRAINT run_records_id_unique IF NOT EXISTS FOR (n:RunRecord) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX run_records_world_id_agent_started_idx IF NOT EXISTS FOR (n:RunRecord) ON (n.world_id, n.agent, n.started);

// model_calls
CREATE CONSTRAINT model_calls_id_unique IF NOT EXISTS FOR (n:ModelCall) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX model_calls_world_id_day_idx IF NOT EXISTS FOR (n:ModelCall) ON (n.world_id, n.day);

// judgments
CREATE CONSTRAINT judgments_id_unique IF NOT EXISTS FOR (n:Judgment) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX judgments_world_id_subject_role_idx IF NOT EXISTS FOR (n:Judgment) ON (n.world_id, n.subject, n.role);
CREATE RANGE INDEX judgments_model_call_id_idx IF NOT EXISTS FOR (n:Judgment) ON (n.model_call_id);

// budgets
CREATE CONSTRAINT budgets_world_id_day_unique IF NOT EXISTS FOR (n:Budget) REQUIRE (n.world_id, n.day) IS UNIQUE;
