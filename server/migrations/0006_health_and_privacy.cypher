// B.1: health and privacy.

// checks
CREATE CONSTRAINT checks_id_unique IF NOT EXISTS FOR (n:Check) REQUIRE n.id IS UNIQUE;

// audit_runs
CREATE CONSTRAINT audit_runs_id_unique IF NOT EXISTS FOR (n:AuditRun) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX audit_runs_check_id_started_idx IF NOT EXISTS FOR (n:AuditRun) ON (n.check_id, n.started);

// audit_grades
CREATE CONSTRAINT audit_grades_world_id_run_id_subject_unique IF NOT EXISTS FOR (n:AuditGrade) REQUIRE (n.world_id, n.run_id, n.subject) IS UNIQUE;

// findings
CREATE CONSTRAINT findings_id_unique IF NOT EXISTS FOR (n:Finding) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX findings_world_id_status_due_idx IF NOT EXISTS FOR (n:Finding) ON (n.world_id, n.status, n.due);

// forget_ledger
CREATE CONSTRAINT forget_ledger_id_unique IF NOT EXISTS FOR (n:ForgetLedger) REQUIRE n.id IS UNIQUE;
CREATE RANGE INDEX forget_ledger_world_id_idx IF NOT EXISTS FOR (n:ForgetLedger) ON (n.world_id);

// oauth_clients
CREATE CONSTRAINT oauth_clients_client_id_unique IF NOT EXISTS FOR (n:OAuthClient) REQUIRE n.client_id IS UNIQUE;
