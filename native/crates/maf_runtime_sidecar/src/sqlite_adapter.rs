use crate::{
    AcknowledgeSubmissionHandoffRequest, AcknowledgeSubmissionProjectionRequest,
    AdmitSubmissionRequest, AdmitSubmissionResponse, AgentItemRecord, AgentRunRecord,
    AgentStateReceipt, ArtifactRecord, BundleRevisionResult, CancellationToken,
    ClaimPendingSubmissionRequest, ClaimPendingSubmissionResponse,
    CloseConversationAdmissionRequest, CloseConversationAdmissionResponse, CommitAgentStateRequest,
    ConversationAdmissionCloseDisposition, EventCursor, GetSubmissionPreparationRequest,
    MessageIdentityDisposition, MessageIdentityKind, MessageIdentityRecord, NodeTransitionResult,
    PrepareSubmissionHandoffRequest, RenewSubmissionClaimRequest, ReserveMessageIdentityRequest,
    ReserveMessageIdentityResponse, SubmissionAdmissionDisposition, SubmissionAdmissionRecord,
    SubmissionClaim, SubmissionHandoffState, SubmissionPreparationState, SubmissionProjectionState,
    TaskNodeRecord, TaskRecord, TaskRouteAssignment, idempotency_conflict, idempotency_key,
    initial_no_server_task_transition_shape, message_identity_is_exact_replay, migration_blocked,
    new_submission_claim, require_idempotency_key, validate_admit_submission_request,
    validate_agent_final_commit_shape, validate_agent_final_projection, validate_agent_item_record,
    validate_agent_item_relationships, validate_agent_item_update, validate_agent_run_record,
    validate_expected_status, validate_message_identity, validate_task_node_record,
    validate_task_node_update, validate_task_record, validate_task_update, write_failed,
};
use maf_runtime_store::{
    PROTO_HASH, RuntimeSidecarError, RuntimeSidecarErrorCode, SCHEMA_HASH, TaskLease,
};
use maf_task_dispatcher::TaskSubmitResult;
use rusqlite::{Connection, OptionalExtension, types::Value};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::Path;
use std::sync::{Mutex, MutexGuard};
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmissionAuthorityFinalizeResult {
    pub exact_replay: bool,
    pub finalization_receipt_sha256: String,
    pub finalization_receipt_json: Vec<u8>,
    pub finalized_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmissionConversationImportRecord {
    pub conversation_id: String,
    pub username: String,
    pub status: String,
    pub active_task_id: Option<String>,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmissionAuthorityImportRequest {
    pub conversations: Vec<SubmissionConversationImportRecord>,
    pub message_identities: Vec<MessageIdentityRecord>,
    pub active_task_ids: Vec<String>,
    pub finalization_subject_json: Vec<u8>,
    pub finalization_receipt_sha256: String,
    pub finalization_receipt_json: Vec<u8>,
    pub finalized_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct SubmissionInventoryEvidence {
    count: u32,
    pk_sha256: String,
    canonical_sha256: String,
    finalize_empty: bool,
}

#[derive(Debug)]
pub struct RuntimeSidecarSqliteAdapter {
    connection: Mutex<Connection>,
}

impl RuntimeSidecarSqliteAdapter {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, RuntimeSidecarError> {
        let connection = Connection::open(path).map_err(|error| {
            sqlite_unavailable("open runtime sidecar SQLite adapter failed", error)
        })?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(|error| sqlite_unavailable("configure SQLite busy timeout failed", error))?;
        let adapter = Self {
            connection: Mutex::new(connection),
        };
        adapter.initialize_schema()?;
        Ok(adapter)
    }

    pub fn open_in_memory() -> Result<Self, RuntimeSidecarError> {
        let connection = Connection::open_in_memory().map_err(|error| {
            sqlite_unavailable(
                "open in-memory runtime sidecar SQLite adapter failed",
                error,
            )
        })?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(|error| sqlite_unavailable("configure SQLite busy timeout failed", error))?;
        let adapter = Self {
            connection: Mutex::new(connection),
        };
        adapter.initialize_schema()?;
        Ok(adapter)
    }

    pub fn commit_agent_state(
        &self,
        request: CommitAgentStateRequest,
    ) -> Result<(AgentRunRecord, Vec<AgentItemRecord>, bool), RuntimeSidecarError> {
        let CommitAgentStateRequest {
            operation,
            run,
            items,
            expected_revision,
            expected_claim_token,
            idempotency,
            task_nodes,
            artifacts,
            final_projection_json,
            task,
        } = request;
        let run = run.ok_or_else(|| write_failed("AgentRunRecord is required"))?;
        let idempotency_key = require_idempotency_key(idempotency_key(idempotency))?;
        validate_agent_run_record(&run)?;
        for item in &items {
            validate_agent_item_record(item, &run)?;
        }
        validate_agent_final_commit_shape(
            &operation,
            &run,
            &items,
            &task_nodes,
            &artifacts,
            task.as_ref(),
            final_projection_json.as_deref(),
        )?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin Agent state transaction failed", error))?;
        if let Some(receipt_json) = transaction
            .query_row(
                "SELECT receipt_json FROM agent_state_idempotency WHERE idempotency_key = ?1",
                rusqlite::params![&idempotency_key],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("read Agent state idempotency failed", error))?
        {
            let receipt: AgentStateReceipt = serde_json::from_str(&receipt_json)
                .map_err(|_| write_failed("decode Agent state receipt failed"))?;
            if receipt.operation != operation || receipt.run != run || receipt.items != items {
                return Err(idempotency_conflict(
                    "agent state idempotency key was reused with different state",
                ));
            }
            if receipt.task_nodes != task_nodes
                || receipt.artifacts != artifacts
                || receipt.final_projection_json != final_projection_json
                || receipt.task != task
            {
                return Err(idempotency_conflict(
                    "agent state idempotency key was reused with different state",
                ));
            }
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent Agent state failed", error))?;
            return Ok((receipt.run, receipt.items, true));
        }
        let existing = transaction
            .query_row(
                "SELECT run_json FROM agent_runs WHERE run_id = ?1",
                rusqlite::params![&run.run_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("read AgentRun failed", error))?
            .map(|payload| {
                serde_json::from_str::<AgentRunRecord>(&payload)
                    .map_err(|_| write_failed("decode AgentRun failed"))
            })
            .transpose()?;
        if operation == "create_run" {
            let task_bound = transaction
                .query_row(
                    "SELECT 1 FROM agent_runs WHERE task_id = ?1",
                    rusqlite::params![&run.task_id],
                    |_| Ok(()),
                )
                .optional()
                .map_err(|error| sqlite_error("check AgentRun Task binding failed", error))?
                .is_some();
            if existing.is_some()
                || task_bound
                || expected_revision != 0
                || run.revision != 0
                || !items.is_empty()
            {
                return Err(write_failed("create_run Agent state conflict"));
            }
        } else {
            let existing = existing.ok_or_else(|| write_failed("AgentRun is missing"))?;
            if existing.task_id != run.task_id
                || existing.conversation_id != run.conversation_id
                || existing.revision != expected_revision
                || existing.claim_token != expected_claim_token
                || run.revision != expected_revision + 1
            {
                return Err(write_failed("Agent state CAS mismatch"));
            }
        }
        let existing_items = agent_items_for_run(&transaction, &run.run_id)?;
        validate_agent_item_relationships(&existing_items, &items)?;
        for item in &items {
            let item_json =
                serde_json::to_string(item).map_err(|_| write_failed("encode AgentItem failed"))?;
            let existing = transaction
                .query_row(
                    "SELECT item_json FROM agent_items WHERE item_id = ?1",
                    rusqlite::params![&item.item_id],
                    |row| row.get::<_, String>(0),
                )
                .optional()
                .map_err(|error| sqlite_error("read AgentItem for update failed", error))?
                .map(|payload| {
                    serde_json::from_str::<AgentItemRecord>(&payload)
                        .map_err(|_| write_failed("decode AgentItem for update failed"))
                })
                .transpose()?;
            if let Some(existing) = existing {
                validate_agent_item_update(&existing, item)?;
                transaction
                    .execute(
                        "UPDATE agent_items SET item_json = ?2 WHERE item_id = ?1",
                        rusqlite::params![&item.item_id, item_json],
                    )
                    .map_err(|error| sqlite_error("update AgentItem failed", error))?;
            } else {
                transaction
                    .execute(
                        "INSERT INTO agent_items (item_id, run_id, task_id, sequence, item_json) VALUES (?1, ?2, ?3, ?4, ?5)",
                        rusqlite::params![&item.item_id, &item.run_id, &item.task_id, item.sequence as i64, item_json],
                    )
                    .map_err(|error| sqlite_error("insert AgentItem failed", error))?;
            }
        }
        for node in &task_nodes {
            validate_task_node_record(node)?;
            if node.task_id != run.task_id {
                return Err(write_failed("Agent TaskNode belongs to a different Task"));
            }
            if let Some(existing) = task_node_by_id(&transaction, &node.node_id)? {
                validate_task_node_update(&existing, node)?;
            }
            let node_json = serde_json::to_string(node)
                .map_err(|_| write_failed("encode Agent TaskNode failed"))?;
            transaction.execute(
                "INSERT INTO task_nodes (node_id, task_id, node_json) VALUES (?1, ?2, ?3) ON CONFLICT(node_id) DO UPDATE SET task_id=excluded.task_id, node_json=excluded.node_json",
                rusqlite::params![&node.node_id, &node.task_id, node_json],
            ).map_err(|error| sqlite_error("upsert Agent TaskNode failed", error))?;
            transaction.execute(
                "INSERT INTO node_statuses (task_id, node_id, status) VALUES (?1, ?2, ?3) ON CONFLICT(task_id, node_id) DO UPDATE SET status=excluded.status",
                rusqlite::params![&node.task_id, &node.node_id, &node.status],
            ).map_err(|error| sqlite_error("upsert Agent TaskNode status failed", error))?;
        }
        for artifact in &artifacts {
            if artifact.artifact_id.is_empty()
                || artifact.task_id != run.task_id
                || artifact.producer_node_id.is_empty()
                || artifact.artifact_type.is_empty()
                || artifact.storage_ref.is_empty()
            {
                return Err(write_failed("Agent Artifact violates contract"));
            }
            if task_node_by_id(&transaction, &artifact.producer_node_id)?.is_none() {
                return Err(write_failed("Agent Artifact producer is missing"));
            }
            transaction.execute(
                "INSERT INTO artifacts (artifact_id, task_id, producer_node_id, artifact_type, storage_ref, summary, is_complete, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                rusqlite::params![&artifact.artifact_id, &artifact.task_id, &artifact.producer_node_id, &artifact.artifact_type, &artifact.storage_ref, &artifact.summary, i64::from(artifact.is_complete), &artifact.created_at],
            ).map_err(|error| sqlite_error("insert Agent Artifact failed", error))?;
        }
        if let Some(projection) = &final_projection_json {
            if operation != "commit_final" {
                return Err(write_failed("Agent final projection operation conflict"));
            }
            validate_agent_final_projection(projection, &run, &task_nodes, &artifacts, &items)?;
            transaction
                .execute(
                    "INSERT INTO agent_final_projections (run_id, projection_json) VALUES (?1, ?2)",
                    rusqlite::params![&run.run_id, projection],
                )
                .map_err(|error| sqlite_error("insert Agent final projection failed", error))?;
        } else if operation == "commit_final" {
            return Err(write_failed("Agent final projection is required"));
        }
        if let Some(task) = &task {
            validate_task_record(task)?;
            if task.task_id != run.task_id || task.conversation_id != run.conversation_id {
                return Err(write_failed("Agent Task projection identity mismatch"));
            }
            if let Some(existing) = task_record_by_id(&transaction, &task.task_id)? {
                validate_task_update_with_submission_context(&transaction, &existing, task)?;
            } else if task.status == "accepted"
                && submission_authority_is_finalized(&transaction)?
                && !submission_or_import_evidence_exists(&transaction, &task.task_id)?
            {
                return Err(migration_blocked(
                    "new accepted Task requires submission admission or import evidence",
                ));
            }
            upsert_task_record(&transaction, task)?;
            release_submission_guard_for_terminal_task(&transaction, task)?;
        }
        let run_json =
            serde_json::to_string(&run).map_err(|_| write_failed("encode AgentRun failed"))?;
        transaction
            .execute(
                "INSERT INTO agent_runs (run_id, task_id, revision, claim_token, run_json) VALUES (?1, ?2, ?3, ?4, ?5) ON CONFLICT(run_id) DO UPDATE SET revision=excluded.revision, claim_token=excluded.claim_token, run_json=excluded.run_json",
                rusqlite::params![&run.run_id, &run.task_id, run.revision as i64, &run.claim_token, run_json],
            )
            .map_err(|error| sqlite_error("upsert AgentRun failed", error))?;
        let receipt = AgentStateReceipt {
            operation,
            run: run.clone(),
            items: items.clone(),
            task_nodes,
            artifacts,
            final_projection_json,
            task,
        };
        let receipt_json = serde_json::to_string(&receipt)
            .map_err(|_| write_failed("encode Agent state receipt failed"))?;
        transaction
            .execute(
                "INSERT INTO agent_state_idempotency (idempotency_key, receipt_json) VALUES (?1, ?2)",
                rusqlite::params![idempotency_key, receipt_json],
            )
            .map_err(|error| sqlite_error("insert Agent state receipt failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit Agent state failed", error))?;
        Ok((run, items, false))
    }

    pub fn get_agent_run(
        &self,
        run_id: &str,
    ) -> Result<Option<AgentRunRecord>, RuntimeSidecarError> {
        self.lock_connection()?
            .query_row(
                "SELECT run_json FROM agent_runs WHERE run_id = ?1",
                rusqlite::params![run_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("read AgentRun failed", error))?
            .map(|payload| {
                serde_json::from_str(&payload).map_err(|_| write_failed("decode AgentRun failed"))
            })
            .transpose()
    }

    pub fn list_agent_runs(
        &self,
        statuses: &std::collections::BTreeSet<String>,
    ) -> Result<Vec<AgentRunRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        let mut statement = connection
            .prepare("SELECT run_json FROM agent_runs ORDER BY run_id")
            .map_err(|error| sqlite_error("prepare AgentRun list failed", error))?;
        let rows = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|error| sqlite_error("query AgentRun list failed", error))?;
        let mut runs = Vec::new();
        for row in rows {
            let payload = row.map_err(|error| sqlite_error("read AgentRun failed", error))?;
            let run: AgentRunRecord = serde_json::from_str(&payload)
                .map_err(|_| write_failed("decode AgentRun failed"))?;
            if statuses.is_empty() || statuses.contains(&run.status) {
                runs.push(run);
            }
        }
        Ok(runs)
    }

    pub fn list_agent_items(
        &self,
        run_id: &str,
    ) -> Result<Vec<AgentItemRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        let mut statement = connection
            .prepare("SELECT item_json FROM agent_items WHERE run_id = ?1 ORDER BY sequence")
            .map_err(|error| sqlite_error("prepare AgentItem list failed", error))?;
        let rows = statement
            .query_map(rusqlite::params![run_id], |row| row.get::<_, String>(0))
            .map_err(|error| sqlite_error("query AgentItem list failed", error))?;
        rows.map(|row| {
            let payload = row.map_err(|error| sqlite_error("read AgentItem failed", error))?;
            serde_json::from_str(&payload).map_err(|_| write_failed("decode AgentItem failed"))
        })
        .collect()
    }

    pub fn get_agent_final_projection(
        &self,
        run_id: &str,
    ) -> Result<Option<Vec<u8>>, RuntimeSidecarError> {
        self.lock_connection()?
            .query_row(
                "SELECT projection_json FROM agent_final_projections WHERE run_id = ?1",
                rusqlite::params![run_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|error| sqlite_error("read Agent final projection failed", error))
    }

    pub fn get_agent_run_for_task(
        &self,
        task_id: &str,
    ) -> Result<Option<AgentRunRecord>, RuntimeSidecarError> {
        self.lock_connection()?
            .query_row(
                "SELECT run_json FROM agent_runs WHERE task_id = ?1",
                rusqlite::params![task_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("read AgentRun for Task failed", error))?
            .map(|payload| {
                serde_json::from_str(&payload)
                    .map_err(|_| write_failed("decode AgentRun for Task failed"))
            })
            .transpose()
    }

    pub fn submit_task(
        &self,
        task_id: &str,
        conversation_id: &str,
        idempotency_key: &str,
    ) -> Result<TaskSubmitResult, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin task submit transaction failed", error))?;
        if let Some(task_id) = submitted_task_for_idempotency(&transaction, &idempotency_key)? {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent task submit failed", error))?;
            return Ok(TaskSubmitResult {
                task_id,
                duplicate: true,
            });
        }
        transaction
            .execute(
                r"
                INSERT INTO submitted_tasks (task_id, conversation_id)
                VALUES (?1, ?2)
                ",
                rusqlite::params![task_id, conversation_id],
            )
            .map_err(|error| sqlite_error("insert submitted task failed", error))?;
        transaction
            .execute(
                r"
                INSERT INTO task_submit_idempotency (idempotency_key, task_id)
                VALUES (?1, ?2)
                ",
                rusqlite::params![&idempotency_key, task_id],
            )
            .map_err(|error| sqlite_error("insert task submit idempotency key failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit task submit failed", error))?;
        Ok(TaskSubmitResult {
            task_id: task_id.to_owned(),
            duplicate: false,
        })
    }

    pub fn submit_task_record(
        &self,
        task: TaskRecord,
        idempotency_key: &str,
        expected_from_status: Option<&str>,
    ) -> Result<(Option<TaskRecord>, TaskSubmitResult), RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        validate_task_record(&task)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin TaskRecord submit transaction failed", error))?;
        if let Some(original) = task_record_for_idempotency(&transaction, &idempotency_key)? {
            if original != task {
                return Err(idempotency_conflict(
                    "task submit idempotency key was reused with a different TaskRecord",
                ));
            }
            transaction.commit().map_err(|error| {
                sqlite_error("commit idempotent TaskRecord submit failed", error)
            })?;
            return Ok((
                Some(original.clone()),
                TaskSubmitResult {
                    task_id: original.task_id,
                    duplicate: true,
                },
            ));
        }
        if let Some(existing) = task_record_by_id(&transaction, &task.task_id)? {
            validate_expected_status(expected_from_status, Some(&existing.status))?;
            validate_task_update_with_submission_context(&transaction, &existing, &task)?;
        } else if submitted_task_conversation_id(&transaction, &task.task_id)?.is_some() {
            return Err(migration_blocked(
                "legacy submitted task requires an explicit audited TaskRecord migration",
            ));
        } else {
            validate_expected_status(expected_from_status, None)?;
            if task.status == "accepted"
                && submission_authority_is_finalized(&transaction)?
                && !submission_or_import_evidence_exists(&transaction, &task.task_id)?
            {
                return Err(migration_blocked(
                    "new accepted Task requires submission admission or import evidence",
                ));
            }
        }
        upsert_task_record(&transaction, &task)?;
        release_submission_guard_for_terminal_task(&transaction, &task)?;
        insert_task_record_idempotency(&transaction, &idempotency_key, &task)?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit TaskRecord submit failed", error))?;
        Ok((
            Some(task.clone()),
            TaskSubmitResult {
                task_id: task.task_id,
                duplicate: false,
            },
        ))
    }

    pub fn get_task(&self, task_id: &str) -> Result<Option<TaskRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        task_record_by_id(&connection, task_id)
    }

    pub fn list_tasks_for_conversation(
        &self,
        conversation_id: &str,
        statuses: &[String],
    ) -> Result<Vec<TaskRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        let mut statement = connection
            .prepare(&format!(
                "SELECT {TASK_RECORD_COLUMNS} FROM submitted_tasks WHERE conversation_id = ?1 AND root_message_id IS NOT NULL ORDER BY created_at DESC, task_id DESC"
            ))
            .map_err(|error| sqlite_error("prepare TaskRecord conversation list failed", error))?;
        let rows = statement
            .query_map(rusqlite::params![conversation_id], task_record_from_row)
            .map_err(|error| sqlite_error("query TaskRecord conversation list failed", error))?;
        let status_filter = statuses.iter().collect::<std::collections::BTreeSet<_>>();
        let mut tasks = Vec::new();
        for row in rows {
            let task = row.map_err(|error| {
                sqlite_error("decode TaskRecord conversation list failed", error)
            })?;
            validate_task_record(&task)?;
            if status_filter.is_empty() || status_filter.contains(&task.status) {
                tasks.push(task);
            }
        }
        Ok(tasks)
    }

    pub fn get_active_task_for_conversation(
        &self,
        conversation_id: &str,
    ) -> Result<Option<TaskRecord>, RuntimeSidecarError> {
        Ok(self
            .list_tasks_for_conversation(
                conversation_id,
                &[
                    "accepted".to_owned(),
                    "planning".to_owned(),
                    "running".to_owned(),
                    "cancelling".to_owned(),
                ],
            )?
            .into_iter()
            .next())
    }

    pub fn transition_node(
        &self,
        task_id: &str,
        node_id: &str,
        to_status: &str,
        expected_from_status: &str,
        idempotency_key: &str,
        node: Option<TaskNodeRecord>,
    ) -> Result<NodeTransitionResult, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin node transition transaction failed", error))?;
        if let Some((result, original_node)) =
            node_transition_for_idempotency(&transaction, &idempotency_key)?
        {
            if original_node != node {
                return Err(idempotency_conflict(
                    "node transition idempotency key was reused with a different TaskNodeRecord",
                ));
            }
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent node transition failed", error))?;
            return Ok(result);
        }
        let existing_node = task_node_by_id(&transaction, node_id)?;
        validate_expected_status(
            Some(expected_from_status),
            existing_node.as_ref().map(|existing| &existing.status),
        )?;
        if let Some(existing) = &existing_node {
            if existing.task_id != task_id {
                return Err(write_failed("TaskNodeRecord cannot move between tasks"));
            }
            if node.is_none() {
                let mut replacement = existing.clone();
                replacement.status = to_status.to_owned();
                validate_task_node_update(existing, &replacement)?;
            }
        }
        if let Some(node) = &node {
            validate_task_node_record(node)?;
            if node.task_id != task_id || node.node_id != node_id || node.status != to_status {
                return Err(crate::write_failed(
                    "TransitionNode identity does not match TaskNodeRecord",
                ));
            }
            if let Some(existing) = existing_node {
                validate_task_node_update(&existing, node)?;
            }
            let node_json = serde_json::to_string(node)
                .map_err(|_| write_failed("encode TaskNodeRecord failed"))?;
            transaction
                .execute(
                    "INSERT INTO task_nodes (node_id, task_id, node_json) VALUES (?1, ?2, ?3) ON CONFLICT(node_id) DO UPDATE SET task_id=excluded.task_id, node_json=excluded.node_json",
                    rusqlite::params![node_id, task_id, node_json],
                )
                .map_err(|error| sqlite_error("upsert TaskNodeRecord failed", error))?;
        }
        transaction
            .execute(
                r"
                INSERT INTO node_statuses (task_id, node_id, status)
                VALUES (?1, ?2, ?3)
                ON CONFLICT(task_id, node_id) DO UPDATE SET
                    status = excluded.status
                ",
                rusqlite::params![task_id, node_id, to_status],
            )
            .map_err(|error| sqlite_error("upsert node status failed", error))?;
        transaction
            .execute(
                r"
                INSERT INTO node_transition_idempotency (
                    idempotency_key,
                    task_id,
                    node_id,
                    status, node_json
                )
                VALUES (?1, ?2, ?3, ?4, ?5)
                ",
                rusqlite::params![
                    &idempotency_key,
                    task_id,
                    node_id,
                    to_status,
                    node.as_ref()
                        .map(serde_json::to_string)
                        .transpose()
                        .map_err(|_| write_failed(
                            "encode TaskNodeRecord idempotency snapshot failed"
                        ))?,
                ],
            )
            .map_err(|error| {
                sqlite_error("insert node transition idempotency key failed", error)
            })?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit node transition failed", error))?;
        Ok(NodeTransitionResult {
            task_id: task_id.to_owned(),
            node_id: node_id.to_owned(),
            status: to_status.to_owned(),
        })
    }

    pub fn get_task_node(
        &self,
        node_id: &str,
    ) -> Result<Option<TaskNodeRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        task_node_by_id(&connection, node_id)
    }

    pub fn list_task_nodes_for_task(
        &self,
        task_id: &str,
    ) -> Result<Vec<TaskNodeRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        let mut statement = connection
            .prepare("SELECT node_json FROM task_nodes WHERE task_id = ?1 ORDER BY node_id")
            .map_err(|error| sqlite_error("prepare TaskNodeRecord list failed", error))?;
        let rows = statement
            .query_map(rusqlite::params![task_id], |row| row.get::<_, String>(0))
            .map_err(|error| sqlite_error("query TaskNodeRecord list failed", error))?;
        rows.map(|row| {
            decode_task_node_json(
                &row.map_err(|error| sqlite_error("read TaskNodeRecord list failed", error))?,
            )
        })
        .collect()
    }

    pub fn save_artifact(
        &self,
        artifact: ArtifactRecord,
        idempotency_key: &str,
    ) -> Result<ArtifactRecord, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin artifact save transaction failed", error))?;
        if let Some(artifact) = artifact_for_idempotency(&transaction, &idempotency_key)? {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent artifact save failed", error))?;
            return Ok(artifact);
        }
        transaction
            .execute(
                r"
                INSERT INTO artifacts (
                    artifact_id,
                    task_id,
                    producer_node_id,
                    artifact_type,
                    storage_ref,
                    summary,
                    is_complete,
                    created_at
                )
                VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    producer_node_id = excluded.producer_node_id,
                    artifact_type = excluded.artifact_type,
                    storage_ref = excluded.storage_ref,
                    summary = excluded.summary,
                    is_complete = excluded.is_complete,
                    created_at = excluded.created_at
                ",
                rusqlite::params![
                    &artifact.artifact_id,
                    &artifact.task_id,
                    &artifact.producer_node_id,
                    &artifact.artifact_type,
                    &artifact.storage_ref,
                    &artifact.summary,
                    if artifact.is_complete { 1 } else { 0 },
                    &artifact.created_at,
                ],
            )
            .map_err(|error| sqlite_error("upsert artifact failed", error))?;
        transaction
            .execute(
                r"
                INSERT INTO artifact_idempotency (
                    idempotency_key,
                    artifact_id,
                    task_id,
                    producer_node_id,
                    artifact_type,
                    storage_ref,
                    summary,
                    is_complete,
                    created_at
                )
                VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
                ",
                rusqlite::params![
                    &idempotency_key,
                    &artifact.artifact_id,
                    &artifact.task_id,
                    &artifact.producer_node_id,
                    &artifact.artifact_type,
                    &artifact.storage_ref,
                    &artifact.summary,
                    if artifact.is_complete { 1 } else { 0 },
                    &artifact.created_at,
                ],
            )
            .map_err(|error| sqlite_error("insert artifact idempotency key failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit artifact save failed", error))?;
        Ok(artifact)
    }

    pub fn get_artifact(
        &self,
        artifact_id: &str,
    ) -> Result<Option<ArtifactRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        connection
            .query_row(
                r"
                SELECT artifact_id, task_id, producer_node_id, artifact_type, storage_ref,
                       summary, is_complete, created_at
                FROM artifacts
                WHERE artifact_id = ?1
                ",
                rusqlite::params![artifact_id],
                artifact_from_row,
            )
            .optional()
            .map_err(|error| sqlite_error("select artifact failed", error))
    }

    pub fn list_artifacts_for_task(
        &self,
        task_id: &str,
    ) -> Result<Vec<ArtifactRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        let mut statement = connection
            .prepare(
                r"
                SELECT artifact_id, task_id, producer_node_id, artifact_type, storage_ref,
                       summary, is_complete, created_at
                FROM artifacts
                WHERE task_id = ?1
                ORDER BY created_at, artifact_id
                ",
            )
            .map_err(|error| sqlite_error("prepare artifact list failed", error))?;
        let rows = statement
            .query_map(rusqlite::params![task_id], artifact_from_row)
            .map_err(|error| sqlite_error("query artifact list failed", error))?;
        let mut artifacts = Vec::new();
        for row in rows {
            artifacts.push(row.map_err(|error| sqlite_error("read artifact row failed", error))?);
        }
        Ok(artifacts)
    }

    pub fn append_event(
        &self,
        conversation_id: &str,
        task_id: &str,
        event_type: &str,
        payload_json: Vec<u8>,
        created_at_ms: i64,
        idempotency_key: &str,
    ) -> Result<EventCursor, RuntimeSidecarError> {
        if payload_json.len() > maf_event_log::MAX_EVENT_PAYLOAD_BYTES {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::EventLogPayloadTooLarge,
                "event payload exceeds configured limit",
            ));
        }
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction().map_err(|error| {
            sqlite_error("begin runtime event append transaction failed", error)
        })?;
        if let Some(cursor) = event_cursor_for_idempotency(&transaction, &idempotency_key)? {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent event append failed", error))?;
            return Ok(cursor);
        }

        let next_sequence: i64 = transaction
            .query_row(
                r"
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM runtime_events
                WHERE conversation_id = ?1 AND task_id = ?2
                ",
                rusqlite::params![conversation_id, task_id],
                |row| row.get(0),
            )
            .map_err(|error| sqlite_error("select next runtime event cursor failed", error))?;
        transaction
            .execute(
                r"
                INSERT INTO runtime_events (
                    conversation_id,
                    task_id,
                    sequence,
                    event_type,
                    payload_json,
                    created_at_ms,
                    idempotency_key
                )
                VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                ",
                rusqlite::params![
                    conversation_id,
                    task_id,
                    next_sequence,
                    event_type,
                    &payload_json,
                    created_at_ms,
                    &idempotency_key
                ],
            )
            .map_err(|error| sqlite_error("insert runtime event failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit runtime event append failed", error))?;

        Ok(EventCursor {
            conversation_id: conversation_id.to_owned(),
            task_id: task_id.to_owned(),
            sequence: next_sequence as u64,
            created_at_ms,
        })
    }

    pub fn replay_events(
        &self,
        conversation_id: &str,
        task_id: &str,
        after_sequence: u64,
        max_events: usize,
        max_bytes: usize,
    ) -> Result<Vec<EventCursor>, RuntimeSidecarError> {
        let max_events = max_events.min(maf_event_log::MAX_REPLAY_EVENTS);
        if max_events == 0 {
            return Ok(Vec::new());
        }
        let max_bytes = max_bytes.min(maf_event_log::MAX_REPLAY_BYTES);
        let connection = self.lock_connection()?;
        let mut statement = connection
            .prepare(
                r"
                SELECT sequence, created_at_ms, length(payload_json)
                FROM runtime_events
                WHERE conversation_id = ?1 AND task_id = ?2 AND sequence > ?3
                ORDER BY sequence ASC
                LIMIT ?4
                ",
            )
            .map_err(|error| sqlite_error("prepare runtime event replay failed", error))?;
        let rows = statement
            .query_map(
                rusqlite::params![
                    conversation_id,
                    task_id,
                    after_sequence as i64,
                    max_events as i64
                ],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                },
            )
            .map_err(|error| sqlite_error("query runtime event replay failed", error))?;

        let mut used_bytes = 0usize;
        let mut cursors = Vec::new();
        for row in rows {
            let (sequence, created_at_ms, payload_bytes) =
                row.map_err(|error| sqlite_error("read runtime event replay row failed", error))?;
            used_bytes += payload_bytes.max(0) as usize;
            if used_bytes > max_bytes {
                return Err(RuntimeSidecarError::new(
                    RuntimeSidecarErrorCode::EventLogReplayPageExceeded,
                    "event replay page exceeds byte limit",
                ));
            }
            cursors.push(EventCursor {
                conversation_id: conversation_id.to_owned(),
                task_id: task_id.to_owned(),
                sequence: sequence as u64,
                created_at_ms,
            });
        }
        Ok(cursors)
    }

    pub fn acquire_lease(
        &self,
        task_id: &str,
        owner_id: &str,
        now_ms: i64,
        ttl_ms: i64,
        idempotency_key: &str,
    ) -> Result<TaskLease, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin task lease acquire transaction failed", error))?;
        if let Some(lease) = lease_for_idempotency(&transaction, &idempotency_key)? {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent lease acquire failed", error))?;
            return Ok(lease);
        }

        let existing = fetch_lease(&transaction, task_id)?;
        if let Some(existing) = existing.as_ref()
            && existing.expires_at_ms > now_ms
            && existing.owner_id != owner_id
        {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseConflict,
                "task lease is owned by another active owner",
            ));
        }
        let revision = existing.as_ref().map_or(1, |lease| lease.revision + 1);
        let lease = TaskLease {
            task_id: task_id.to_owned(),
            owner_id: owner_id.to_owned(),
            revision,
            expires_at_ms: now_ms + ttl_ms,
            renew_token: format!("lease:{task_id}:{owner_id}:{revision}"),
        };
        upsert_lease(&transaction, &lease)?;
        transaction
            .execute(
                r"
                INSERT INTO lease_idempotency (
                    idempotency_key,
                    task_id,
                    owner_id,
                    revision,
                    expires_at_ms,
                    renew_token
                )
                VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                ",
                rusqlite::params![
                    &idempotency_key,
                    &lease.task_id,
                    &lease.owner_id,
                    lease.revision as i64,
                    lease.expires_at_ms,
                    &lease.renew_token,
                ],
            )
            .map_err(|error| sqlite_error("insert task lease idempotency key failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit task lease acquire failed", error))?;
        Ok(lease)
    }

    pub fn renew_lease(
        &self,
        task_id: &str,
        renew_token: &str,
        now_ms: i64,
        ttl_ms: i64,
    ) -> Result<TaskLease, RuntimeSidecarError> {
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin task lease renew transaction failed", error))?;
        let mut lease = fetch_lease(&transaction, task_id)?.ok_or_else(|| {
            RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseExpired,
                "task lease is missing",
            )
        })?;
        if lease.renew_token != renew_token || lease.expires_at_ms <= now_ms {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseExpired,
                "task lease cannot be renewed",
            ));
        }
        lease.revision += 1;
        lease.expires_at_ms = now_ms + ttl_ms;
        lease.renew_token = format!("lease:{task_id}:{}:{}", lease.owner_id, lease.revision);
        upsert_lease(&transaction, &lease)?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit task lease renew failed", error))?;
        Ok(lease)
    }

    pub fn release_lease(
        &self,
        task_id: &str,
        renew_token: &str,
    ) -> Result<bool, RuntimeSidecarError> {
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin task lease release transaction failed", error))?;
        let Some(lease) = fetch_lease(&transaction, task_id)? else {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit missing lease release failed", error))?;
            return Ok(false);
        };
        if lease.renew_token != renew_token {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseConflict,
                "task lease release token mismatch",
            ));
        }
        transaction
            .execute(
                "DELETE FROM task_leases WHERE task_id = ?1",
                rusqlite::params![task_id],
            )
            .map_err(|error| sqlite_error("delete task lease failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit task lease release failed", error))?;
        Ok(true)
    }

    pub fn write_cancellation_token(
        &self,
        task_id: &str,
        requested_at_ms: i64,
        reason: &str,
        terminal_policy: &str,
        idempotency_key: &str,
    ) -> Result<bool, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction().map_err(|error| {
            sqlite_error("begin cancellation token write transaction failed", error)
        })?;
        if let Some(written) = cancellation_written_for_idempotency(&transaction, &idempotency_key)?
        {
            transaction.commit().map_err(|error| {
                sqlite_error("commit idempotent cancellation token write failed", error)
            })?;
            return Ok(written);
        }
        transaction
            .execute(
                r"
                INSERT INTO cancellation_tokens (
                    task_id,
                    requested_at_ms,
                    reason,
                    terminal_policy
                )
                VALUES (?1, ?2, ?3, ?4)
                ON CONFLICT(task_id) DO UPDATE SET
                    requested_at_ms = excluded.requested_at_ms,
                    reason = excluded.reason,
                    terminal_policy = excluded.terminal_policy
                ",
                rusqlite::params![task_id, requested_at_ms, reason, terminal_policy],
            )
            .map_err(|error| sqlite_error("upsert cancellation token failed", error))?;
        transaction
            .execute(
                r"
                INSERT INTO cancellation_idempotency (idempotency_key, task_id, written)
                VALUES (?1, ?2, 1)
                ",
                rusqlite::params![&idempotency_key, task_id],
            )
            .map_err(|error| {
                sqlite_error("insert cancellation token idempotency key failed", error)
            })?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit cancellation token write failed", error))?;
        Ok(true)
    }

    pub fn cancellation_token(
        &self,
        task_id: &str,
    ) -> Result<Option<CancellationToken>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        fetch_cancellation_token(&connection, task_id)
    }

    pub fn pin_bundle_revision(
        &self,
        task_id: &str,
        bundle_kind: &str,
        revision: &str,
        idempotency_key: &str,
    ) -> Result<BundleRevisionResult, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin bundle revision pin transaction failed", error))?;
        if let Some(result) = bundle_revision_for_idempotency(&transaction, &idempotency_key)? {
            transaction.commit().map_err(|error| {
                sqlite_error("commit idempotent bundle revision pin failed", error)
            })?;
            return Ok(result);
        }
        transaction
            .execute(
                r"
                INSERT INTO bundle_pins (task_id, bundle_kind, revision, released_at_ms)
                VALUES (?1, ?2, ?3, NULL)
                ON CONFLICT(task_id, bundle_kind) DO UPDATE SET
                    revision = excluded.revision,
                    released_at_ms = NULL
                ",
                rusqlite::params![task_id, bundle_kind, revision],
            )
            .map_err(|error| sqlite_error("upsert bundle revision pin failed", error))?;
        insert_bundle_revision_idempotency(
            &transaction,
            &idempotency_key,
            task_id,
            bundle_kind,
            revision,
            false,
        )?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit bundle revision pin failed", error))?;
        Ok(BundleRevisionResult {
            task_id: task_id.to_owned(),
            bundle_kind: bundle_kind.to_owned(),
            revision: revision.to_owned(),
            released: false,
        })
    }

    pub fn release_bundle_revision(
        &self,
        task_id: &str,
        bundle_kind: &str,
        revision: &str,
        released_at_ms: i64,
        idempotency_key: &str,
    ) -> Result<BundleRevisionResult, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction().map_err(|error| {
            sqlite_error("begin bundle revision release transaction failed", error)
        })?;
        if let Some(result) = bundle_revision_for_idempotency(&transaction, &idempotency_key)? {
            transaction.commit().map_err(|error| {
                sqlite_error("commit idempotent bundle revision release failed", error)
            })?;
            return Ok(result);
        }
        let Some(active_pin) = fetch_bundle_pin(&transaction, task_id, bundle_kind)? else {
            return Err(write_failed("bundle revision pin is missing"));
        };
        if active_pin.revision != revision {
            return Err(write_failed("bundle revision does not match active pin"));
        }
        transaction
            .execute(
                r"
                UPDATE bundle_pins
                SET released_at_ms = ?4
                WHERE task_id = ?1 AND bundle_kind = ?2 AND revision = ?3
                ",
                rusqlite::params![task_id, bundle_kind, revision, released_at_ms],
            )
            .map_err(|error| sqlite_error("update bundle revision release failed", error))?;
        insert_bundle_revision_idempotency(
            &transaction,
            &idempotency_key,
            task_id,
            bundle_kind,
            revision,
            true,
        )?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit bundle revision release failed", error))?;
        Ok(BundleRevisionResult {
            task_id: task_id.to_owned(),
            bundle_kind: bundle_kind.to_owned(),
            revision: revision.to_owned(),
            released: true,
        })
    }

    pub fn finalize_empty_submission_authority(
        &self,
        finalization_receipt_sha256: &str,
        finalization_subject_json: &[u8],
        finalization_receipt_json: &[u8],
        finalized_at_ms: i64,
    ) -> Result<SubmissionAuthorityFinalizeResult, RuntimeSidecarError> {
        if !is_lower_sha256(finalization_receipt_sha256) {
            return Err(write_failed(
                "submission authority finalization receipt digest is invalid",
            ));
        }
        validate_finalization_receipt(
            finalization_receipt_sha256,
            finalization_receipt_json,
            finalized_at_ms,
        )?;
        validate_receipt_inventory_evidence(
            finalization_receipt_json,
            &empty_inventory_evidence("conversations"),
            &empty_inventory_evidence("message_identities"),
            &empty_inventory_evidence("active_tasks"),
        )?;
        validate_finalization_subject(
            finalization_subject_json,
            finalization_receipt_sha256,
            &empty_inventory_evidence("conversations"),
            &empty_inventory_evidence("message_identities"),
            &empty_inventory_evidence("active_tasks"),
        )?;
        validate_receipt_subject_binding(finalization_receipt_json, finalization_subject_json)?;
        let mut connection = self.lock_connection()?;
        if let Some(result) =
            finalized_submission_authority_replay(&connection, finalization_receipt_sha256)?
        {
            return Ok(result);
        }
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| {
                sqlite_error("begin submission authority finalization failed", error)
            })?;
        if let Some(result) =
            finalized_submission_authority_replay(&transaction, finalization_receipt_sha256)?
        {
            transaction.commit().map_err(|error| {
                sqlite_error(
                    "commit exact submission authority finalization replay failed",
                    error,
                )
            })?;
            return Ok(result);
        }
        let inventory_count: i64 = transaction
            .query_row(
                "SELECT (SELECT count(*) FROM submission_conversations) + (SELECT count(*) FROM submission_message_identities) + (SELECT count(*) FROM submission_admissions)",
                [],
                |row| row.get(0),
            )
            .map_err(|error| sqlite_error("read submission authority inventory failed", error))?;
        if inventory_count != 0 {
            return Err(write_failed(
                "finalize-empty requires an empty submission authority inventory",
            ));
        }
        transaction
            .execute(
                "UPDATE submission_authority_meta SET state='finalized', finalization_receipt_sha256=?1, finalization_receipt_json=?2, finalized_at_ms=?3 WHERE singleton_key=1 AND state='uninitialized'",
                rusqlite::params![finalization_receipt_sha256, finalization_receipt_json, finalized_at_ms],
            )
            .map_err(|error| sqlite_error("finalize submission authority failed", error))?;
        transaction.commit().map_err(|error| {
            sqlite_error("commit submission authority finalization failed", error)
        })?;
        Ok(SubmissionAuthorityFinalizeResult {
            exact_replay: false,
            finalization_receipt_sha256: finalization_receipt_sha256.to_owned(),
            finalization_receipt_json: finalization_receipt_json.to_vec(),
            finalized_at_ms,
        })
    }

    pub fn import_and_finalize_submission_authority(
        &self,
        request: SubmissionAuthorityImportRequest,
    ) -> Result<SubmissionAuthorityFinalizeResult, RuntimeSidecarError> {
        if !is_lower_sha256(&request.finalization_receipt_sha256) {
            return Err(write_failed(
                "submission authority finalization receipt digest is invalid",
            ));
        }
        validate_finalization_receipt(
            &request.finalization_receipt_sha256,
            &request.finalization_receipt_json,
            request.finalized_at_ms,
        )?;
        validate_import_records(&request)?;
        let conversation_inventory = conversation_inventory_evidence(&request.conversations)?;
        let message_inventory = message_identity_inventory_evidence(&request.message_identities)?;
        let declared_active_inventory = finalization_subject_inventory(
            &request.finalization_subject_json,
            "active_task_inventory",
        )?;
        validate_active_task_id_inventory(&request.active_task_ids, &declared_active_inventory)?;
        validate_finalization_subject(
            &request.finalization_subject_json,
            &request.finalization_receipt_sha256,
            &conversation_inventory,
            &message_inventory,
            &declared_active_inventory,
        )?;
        validate_receipt_inventory_evidence(
            &request.finalization_receipt_json,
            &conversation_inventory,
            &message_inventory,
            &declared_active_inventory,
        )?;
        validate_receipt_subject_binding(
            &request.finalization_receipt_json,
            &request.finalization_subject_json,
        )?;
        let mut connection = self.lock_connection()?;
        if let Some(result) = finalized_submission_authority_replay(
            &connection,
            &request.finalization_receipt_sha256,
        )? {
            return Ok(result);
        }
        let active_inventory = active_task_inventory_evidence(&connection)?;
        if active_inventory != declared_active_inventory {
            return Err(write_failed(
                "submission authority active Task inventory digest mismatch",
            ));
        }
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin submission authority import failed", error))?;
        if let Some(result) = finalized_submission_authority_replay(
            &transaction,
            &request.finalization_receipt_sha256,
        )? {
            transaction.commit().map_err(|error| {
                sqlite_error(
                    "commit exact submission authority import replay failed",
                    error,
                )
            })?;
            return Ok(result);
        }
        let locked_active_inventory = active_task_inventory_evidence(&transaction)?;
        if locked_active_inventory != active_inventory {
            return Err(write_failed(
                "submission authority active Task inventory changed before import",
            ));
        }
        let existing_count: i64 = transaction
            .query_row(
                "SELECT (SELECT count(*) FROM submission_conversations) + (SELECT count(*) FROM submission_message_identities) + (SELECT count(*) FROM submission_admissions)",
                [],
                |row| row.get(0),
            )
            .map_err(|error| sqlite_error("read import destination inventory failed", error))?;
        if existing_count != 0 {
            return Err(write_failed(
                "submission authority import destination is not empty",
            ));
        }
        let active_tasks = active_task_ids(&transaction)?;
        let requested_active_tasks = request
            .active_task_ids
            .iter()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>();
        if active_tasks != requested_active_tasks
            || requested_active_tasks.len() != request.active_task_ids.len()
        {
            return Err(write_failed(
                "submission authority active Task inventory mismatch",
            ));
        }
        for conversation in &request.conversations {
            if let Some(task_id) = &conversation.active_task_id {
                let task = task_record_by_id(&transaction, task_id)?
                    .ok_or_else(|| write_failed("imported active Task is missing"))?;
                if task.conversation_id != conversation.conversation_id {
                    return Err(write_failed(
                        "imported active Task belongs to a different Conversation",
                    ));
                }
            }
            transaction
                .execute(
                    "INSERT INTO submission_conversations(conversation_id, username, status, revision, active_task_id, close_operation_id, updated_at_ms) VALUES (?1, ?2, ?3, 1, ?4, NULL, ?5)",
                    rusqlite::params![&conversation.conversation_id, &conversation.username, &conversation.status, &conversation.active_task_id, conversation.updated_at_ms],
                )
                .map_err(|error| sqlite_error("import submission Conversation guard failed", error))?;
        }
        for identity in &request.message_identities {
            insert_message_identity(&transaction, identity)?;
        }
        transaction
            .execute(
                "UPDATE submission_authority_meta SET state='finalized', finalization_receipt_sha256=?1, finalization_receipt_json=?2, finalized_at_ms=?3 WHERE singleton_key=1 AND state='uninitialized'",
                rusqlite::params![&request.finalization_receipt_sha256, &request.finalization_receipt_json, request.finalized_at_ms],
            )
            .map_err(|error| sqlite_error("finalize imported submission authority failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit submission authority import failed", error))?;
        Ok(SubmissionAuthorityFinalizeResult {
            exact_replay: false,
            finalization_receipt_sha256: request.finalization_receipt_sha256,
            finalization_receipt_json: request.finalization_receipt_json,
            finalized_at_ms: request.finalized_at_ms,
        })
    }

    pub fn admit_submission(
        &self,
        request: AdmitSubmissionRequest,
    ) -> Result<AdmitSubmissionResponse, RuntimeSidecarError> {
        validate_admit_submission_request(&request)?;
        let message_projection: serde_json::Value =
            serde_json::from_slice(&request.message_projection_json)
                .map_err(|_| write_failed("message projection JSON is invalid"))?;
        let message_role = message_projection["role"]
            .as_str()
            .ok_or_else(|| write_failed("message projection role is invalid"))?;
        let message_type = message_projection["message_type"]
            .as_str()
            .ok_or_else(|| write_failed("message projection type is invalid"))?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin submission admission failed", error))?;
        require_finalized_submission_authority(&transaction)?;

        if let Some(identity) = message_identity_by_id(&transaction, &request.message_id)? {
            let is_replay = identity.identity_kind == MessageIdentityKind::Submission
                && identity.conversation_id == request.conversation_id
                && identity.username == request.username
                && identity.role.as_deref() == Some(message_role)
                && identity.message_type.as_deref() == Some(message_type)
                && identity.request_fingerprint.as_deref()
                    == Some(request.request_fingerprint.as_str());
            if !is_replay {
                return Ok(admission_disposition(
                    SubmissionAdmissionDisposition::MessageIdConflict,
                ));
            }
            let (record, claim) = admission_by_message_id(&transaction, &request.message_id)?
                .ok_or_else(|| write_failed("submission identity is missing its admission"))?;
            if record.idempotency_key != request.idempotency_key {
                return Ok(admission_disposition(
                    SubmissionAdmissionDisposition::MessageIdConflict,
                ));
            }
            let visible_claim = claim.filter(|claim| {
                claim.owner == request.workflow_owner && claim.expires_at_ms > request.now_ms
            });
            transaction.commit().map_err(|error| {
                sqlite_error("commit exact submission admission replay failed", error)
            })?;
            return Ok(AdmitSubmissionResponse {
                disposition: SubmissionAdmissionDisposition::IdempotentReplay,
                admission: Some(record),
                claim: visible_claim,
                error: None,
            });
        }

        if transaction
            .query_row(
                "SELECT message_id FROM submission_admissions WHERE idempotency_key=?1",
                rusqlite::params![&request.idempotency_key],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("read submission idempotency identity failed", error))?
            .is_some()
        {
            return Err(idempotency_conflict(
                "submission idempotency key is bound to a different Message",
            ));
        }

        transaction
            .execute(
                "INSERT OR IGNORE INTO submission_conversations(conversation_id, username, status, revision, active_task_id, close_operation_id, updated_at_ms) VALUES (?1, ?2, 'active', 1, NULL, NULL, ?3)",
                rusqlite::params![&request.conversation_id, &request.username, request.now_ms],
            )
            .map_err(|error| sqlite_error("create submission Conversation guard failed", error))?;
        let (guard_username, guard_status, active_task_id): (String, String, Option<String>) =
            transaction
                .query_row(
                    "SELECT username, status, active_task_id FROM submission_conversations WHERE conversation_id=?1",
                    rusqlite::params![&request.conversation_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .map_err(|error| sqlite_error("read submission Conversation guard failed", error))?;
        if guard_username != request.username || guard_status != "active" {
            return Ok(admission_disposition(
                SubmissionAdmissionDisposition::ConversationNotAvailable,
            ));
        }
        if active_task_id.is_some() {
            return Ok(admission_disposition(
                SubmissionAdmissionDisposition::ConversationBusy,
            ));
        }
        if task_record_by_id(&transaction, &request.task_id)?.is_some()
            || transaction
                .query_row(
                    "SELECT 1 FROM submission_admissions WHERE task_id=?1",
                    rusqlite::params![&request.task_id],
                    |_| Ok(()),
                )
                .optional()
                .map_err(|error| sqlite_error("check submission Task identity failed", error))?
                .is_some()
        {
            return Err(idempotency_conflict(
                "submission Task identity is already authoritative",
            ));
        }

        let claim = new_submission_claim(
            &request.message_id,
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
            1,
        )?;
        let identity = MessageIdentityRecord {
            message_id: request.message_id.clone(),
            conversation_id: request.conversation_id.clone(),
            username: request.username.clone(),
            identity_kind: MessageIdentityKind::Submission,
            role: Some(message_role.to_owned()),
            message_type: Some(message_type.to_owned()),
            message_created_at_ms: Some(request.message_created_at_ms),
            task_id: Some(request.task_id.clone()),
            request_fingerprint: Some(request.request_fingerprint.clone()),
            reserved_at_ms: request.now_ms,
        };
        insert_message_identity(&transaction, &identity)?;
        upsert_task_record(&transaction, &request.task)?;
        insert_task_record_idempotency(&transaction, &request.idempotency_key, &request.task)?;
        transaction
            .execute(
                r"INSERT INTO submission_admissions(
                    message_id, task_id, conversation_id, username, idempotency_key,
                    request_fingerprint, conversation_projection_json, message_projection_json,
                    projection_sha256, continuation_json, continuation_sha256, admission_state,
                    projection_state, preparation_state, prepared_execution_json,
                    prepared_execution_sha256, handoff_state, handoff_kind, handoff_identity,
                    claim_owner, claim_token, claim_expires_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'open',
                    'pending', 'pending', NULL, NULL, 'pending', NULL, NULL, ?12, ?13, ?14, ?15, ?15)",
                rusqlite::params![
                    &request.message_id,
                    &request.task_id,
                    &request.conversation_id,
                    &request.username,
                    &request.idempotency_key,
                    &request.request_fingerprint,
                    &request.conversation_projection_json,
                    &request.message_projection_json,
                    &request.projection_sha256,
                    &request.continuation_json,
                    &request.continuation_sha256,
                    &claim.owner,
                    &claim.token,
                    claim.expires_at_ms,
                    request.now_ms,
                ],
            )
            .map_err(|error| sqlite_error("insert submission admission failed", error))?;
        transaction
            .execute(
                "UPDATE submission_conversations SET active_task_id=?2, revision=revision+1, updated_at_ms=?3 WHERE conversation_id=?1 AND status='active' AND active_task_id IS NULL",
                rusqlite::params![&request.conversation_id, &request.task_id, request.now_ms],
            )
            .map_err(|error| sqlite_error("activate submission Conversation guard failed", error))?;
        let record = admission_by_message_id(&transaction, &request.message_id)?
            .ok_or_else(|| write_failed("created submission admission is missing"))?
            .0;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit submission admission failed", error))?;
        Ok(AdmitSubmissionResponse {
            disposition: SubmissionAdmissionDisposition::Created,
            admission: Some(record),
            claim: Some(claim),
            error: None,
        })
    }

    pub fn claim_pending_submission(
        &self,
        request: ClaimPendingSubmissionRequest,
    ) -> Result<ClaimPendingSubmissionResponse, RuntimeSidecarError> {
        validate_claim_request(&request)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin pending submission claim failed", error))?;
        let receipt = require_finalized_submission_authority(&transaction)?;
        let candidate = transaction
            .query_row(
                r"SELECT message_id FROM submission_admissions
                  WHERE admission_state='open' AND handoff_state='pending'
                    AND (claim_token IS NULL OR claim_expires_at_ms <= ?1)
                    AND (?2 IS NULL OR (created_at_ms > ?2 OR (created_at_ms = ?2 AND message_id > ?3)))
                  ORDER BY created_at_ms, message_id LIMIT 1",
                rusqlite::params![request.now_ms, request.after_created_at_ms, request.after_message_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("scan pending submission failed", error))?;
        let Some(message_id) = candidate else {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit empty submission claim failed", error))?;
            return Ok(ClaimPendingSubmissionResponse {
                found: false,
                admission: None,
                claim: None,
                authority_state: "finalized".to_owned(),
                finalization_receipt_sha256: Some(receipt),
                error: None,
            });
        };
        let claim = new_submission_claim(
            &message_id,
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
            request.now_ms as u64 + 1,
        )?;
        let updated = transaction
            .execute(
                r"UPDATE submission_admissions SET claim_owner=?2, claim_token=?3,
                   claim_expires_at_ms=?4
                   WHERE message_id=?1 AND admission_state='open' AND handoff_state='pending'
                     AND (claim_token IS NULL OR claim_expires_at_ms <= ?5)",
                rusqlite::params![
                    &message_id,
                    &claim.owner,
                    &claim.token,
                    claim.expires_at_ms,
                    request.now_ms
                ],
            )
            .map_err(|error| sqlite_error("claim pending submission failed", error))?;
        if updated != 1 {
            return Err(write_failed("pending submission claim lost its CAS"));
        }
        let record = admission_by_message_id(&transaction, &message_id)?
            .ok_or_else(|| write_failed("claimed submission disappeared"))?
            .0;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit pending submission claim failed", error))?;
        Ok(ClaimPendingSubmissionResponse {
            found: true,
            admission: Some(record),
            claim: Some(claim),
            authority_state: "finalized".to_owned(),
            finalization_receipt_sha256: Some(receipt),
            error: None,
        })
    }

    pub fn renew_submission_claim(
        &self,
        request: RenewSubmissionClaimRequest,
    ) -> Result<SubmissionClaim, RuntimeSidecarError> {
        if request.workflow_owner.trim().is_empty()
            || request.now_ms < 0
            || request.claim_ttl_ms <= 0
        {
            return Err(write_failed("submission claim timing or owner is invalid"));
        }
        let claim = new_submission_claim(
            &request.message_id,
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
            request.now_ms as u64 + 1,
        )?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin submission claim renewal failed", error))?;
        require_finalized_submission_authority(&transaction)?;
        let updated = transaction
            .execute(
                r"UPDATE submission_admissions SET claim_token=?4, claim_expires_at_ms=?5
                   WHERE message_id=?1 AND claim_owner=?2 AND claim_token=?3
                   AND claim_expires_at_ms > ?6 AND admission_state='open' AND handoff_state='pending'",
                rusqlite::params![
                    &request.message_id,
                    &request.workflow_owner,
                    &request.claim_token,
                    &claim.token,
                    claim.expires_at_ms,
                    request.now_ms,
                ],
            )
            .map_err(|error| sqlite_error("renew submission claim failed", error))?;
        if updated != 1 {
            return Err(idempotency_conflict(
                "submission claim owner, token, expiry, or phase mismatch",
            ));
        }
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit submission claim renewal failed", error))?;
        Ok(claim)
    }

    pub fn acknowledge_submission_projection(
        &self,
        request: AcknowledgeSubmissionProjectionRequest,
    ) -> Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError> {
        self.update_admission_with_claim(
            &request.message_id,
            &request.workflow_owner,
            &request.claim_token,
            request.now_ms,
            |transaction, record| {
                if record.projection_sha256 != request.projection_sha256 {
                    return Err(idempotency_conflict("submission projection digest mismatch"));
                }
                if record.projection_state == SubmissionProjectionState::Projected {
                    return Ok(true);
                }
                if request.expected_state != SubmissionProjectionState::Pending {
                    return Err(idempotency_conflict(
                        "submission projection expected state mismatch",
                    ));
                }
                transaction
                    .execute(
                        "UPDATE submission_admissions SET projection_state='projected', updated_at_ms=?2 WHERE message_id=?1",
                        rusqlite::params![&request.message_id, request.now_ms],
                    )
                    .map_err(|error| sqlite_error("acknowledge submission projection failed", error))?;
                Ok(false)
            },
        )
    }

    pub fn prepare_submission_handoff(
        &self,
        request: PrepareSubmissionHandoffRequest,
    ) -> Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError> {
        crate::validate_prepared_execution(
            &request.prepared_execution_json,
            &request.prepared_execution_sha256,
        )?;
        let prepared: serde_json::Value = serde_json::from_slice(&request.prepared_execution_json)
            .map_err(|_| write_failed("prepared execution JSON is invalid"))?;
        self.update_admission_with_claim(
            &request.message_id,
            &request.workflow_owner,
            &request.claim_token,
            request.now_ms,
            |transaction, record| {
                if prepared["message_id"].as_str() != Some(record.message_id.as_str())
                    || prepared["task_id"].as_str() != Some(record.task_id.as_str())
                    || prepared["conversation_id"].as_str()
                        != Some(record.conversation_id.as_str())
                    || prepared["owner_scope"].as_str() != Some(record.username.as_str())
                    || prepared["requested_capability_id"].as_str()
                        != record.task.requested_capability_id.as_deref()
                {
                    return Err(write_failed("prepared execution identity mismatch"));
                }
                if record.projection_state != SubmissionProjectionState::Projected {
                    return Err(idempotency_conflict(
                        "submission must be projected before preparation",
                    ));
                }
                if record.preparation_state == SubmissionPreparationState::Prepared {
                    if record.prepared_execution_json.as_deref()
                        != Some(request.prepared_execution_json.as_slice())
                        || record.prepared_execution_sha256.as_deref()
                            != Some(request.prepared_execution_sha256.as_str())
                    {
                        return Err(idempotency_conflict(
                            "submission prepared snapshot is immutable",
                        ));
                    }
                    return Ok(true);
                }
                if request.expected_state != SubmissionPreparationState::Pending {
                    return Err(idempotency_conflict(
                        "submission preparation expected state mismatch",
                    ));
                }
                transaction
                    .execute(
                        "UPDATE submission_admissions SET preparation_state='prepared', prepared_execution_json=?2, prepared_execution_sha256=?3, updated_at_ms=?4 WHERE message_id=?1",
                        rusqlite::params![&request.message_id, &request.prepared_execution_json, &request.prepared_execution_sha256, request.now_ms],
                    )
                    .map_err(|error| sqlite_error("prepare submission handoff failed", error))?;
                Ok(false)
            },
        )
    }

    pub fn get_submission_preparation(
        &self,
        request: &GetSubmissionPreparationRequest,
    ) -> Result<Option<SubmissionAdmissionRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        require_finalized_submission_authority(&connection)?;
        let row = connection
            .query_row(
                "SELECT message_id FROM submission_admissions WHERE username=?1 AND conversation_id=?2 AND task_id=?3 AND preparation_state='prepared'",
                rusqlite::params![&request.username, &request.conversation_id, &request.task_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("read prepared submission identity failed", error))?;
        row.map(|message_id| {
            admission_by_message_id(&connection, &message_id).map(|value| value.map(|item| item.0))
        })
        .transpose()
        .map(Option::flatten)
    }

    pub fn acknowledge_submission_handoff(
        &self,
        request: AcknowledgeSubmissionHandoffRequest,
    ) -> Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError> {
        if !matches!(
            request.handoff_kind.as_str(),
            "agent_run" | "interrupt" | "no_server_intent"
        ) || request.handoff_identity.trim().is_empty()
        {
            return Err(write_failed("submission handoff identity is invalid"));
        }
        self.update_admission_with_claim(
            &request.message_id,
            &request.workflow_owner,
            &request.claim_token,
            request.now_ms,
            |transaction, record| {
                if record.preparation_state != SubmissionPreparationState::Prepared
                    || record.prepared_execution_sha256.as_deref()
                        != Some(request.prepared_execution_sha256.as_str())
                {
                    return Err(idempotency_conflict(
                        "submission prepared handoff digest mismatch",
                    ));
                }
                if record.handoff_state == SubmissionHandoffState::HandedOff {
                    if record.handoff_kind.as_deref() != Some(request.handoff_kind.as_str())
                        || record.handoff_identity.as_deref()
                            != Some(request.handoff_identity.as_str())
                    {
                        return Err(idempotency_conflict(
                            "submission durable handoff is immutable",
                        ));
                    }
                    return Ok(true);
                }
                if request.expected_state != SubmissionHandoffState::Pending {
                    return Err(idempotency_conflict(
                        "submission handoff expected state mismatch",
                    ));
                }
                transaction
                    .execute(
                        "UPDATE submission_admissions SET handoff_state='handed_off', handoff_kind=?2, handoff_identity=?3, updated_at_ms=?4 WHERE message_id=?1",
                        rusqlite::params![&request.message_id, &request.handoff_kind, &request.handoff_identity, request.now_ms],
                    )
                    .map_err(|error| sqlite_error("acknowledge submission handoff failed", error))?;
                Ok(false)
            },
        )
    }

    fn update_admission_with_claim(
        &self,
        message_id: &str,
        owner: &str,
        token: &str,
        now_ms: i64,
        update: impl FnOnce(
            &Connection,
            &SubmissionAdmissionRecord,
        ) -> Result<bool, RuntimeSidecarError>,
    ) -> Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError> {
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin submission phase update failed", error))?;
        require_finalized_submission_authority(&transaction)?;
        let (record, claim) = admission_by_message_id(&transaction, message_id)?
            .ok_or_else(|| idempotency_conflict("submission admission is unknown"))?;
        let claim = claim
            .ok_or_else(|| idempotency_conflict("submission admission has no active claim"))?;
        if record.closed
            || claim.owner != owner
            || claim.token != token
            || claim.expires_at_ms <= now_ms
        {
            return Err(idempotency_conflict(
                "submission claim owner, token, expiry, or phase mismatch",
            ));
        }
        let duplicate = update(&transaction, &record)?;
        let updated = admission_by_message_id(&transaction, message_id)?
            .ok_or_else(|| write_failed("updated submission admission disappeared"))?
            .0;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit submission phase update failed", error))?;
        Ok((updated, duplicate))
    }

    pub fn close_conversation_admission(
        &self,
        request: CloseConversationAdmissionRequest,
    ) -> Result<CloseConversationAdmissionResponse, RuntimeSidecarError> {
        if request.username.trim().is_empty()
            || request.conversation_id.trim().is_empty()
            || request.operation_id.trim().is_empty()
            || request.now_ms < 0
        {
            return Err(write_failed("conversation close identity is invalid"));
        }
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin Conversation admission close failed", error))?;
        require_finalized_submission_authority(&transaction)?;
        let guard = transaction
            .query_row(
                "SELECT username, status, revision, active_task_id, close_operation_id FROM submission_conversations WHERE conversation_id=?1",
                rusqlite::params![&request.conversation_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, i64>(2)?, row.get::<_, Option<String>>(3)?, row.get::<_, Option<String>>(4)?)),
            )
            .optional()
            .map_err(|error| sqlite_error("read Conversation admission guard failed", error))?;
        let Some((username, status, revision, active_task_id, close_operation_id)) = guard else {
            return Ok(CloseConversationAdmissionResponse {
                disposition: ConversationAdmissionCloseDisposition::ConversationNotAvailable,
                revision: 0,
                error: None,
            });
        };
        if username != request.username {
            return Ok(CloseConversationAdmissionResponse {
                disposition: ConversationAdmissionCloseDisposition::ConversationNotAvailable,
                revision: revision as u64,
                error: None,
            });
        }
        if status == "unavailable" {
            return Ok(CloseConversationAdmissionResponse {
                disposition: if close_operation_id.as_deref() == Some(request.operation_id.as_str())
                {
                    ConversationAdmissionCloseDisposition::ExactReplay
                } else {
                    ConversationAdmissionCloseDisposition::Conflict
                },
                revision: revision as u64,
                error: None,
            });
        }
        let new_revision = revision + 1;
        transaction
            .execute(
                "UPDATE submission_conversations SET status='unavailable', revision=?2, active_task_id=NULL, close_operation_id=?3, updated_at_ms=?4 WHERE conversation_id=?1 AND status='active' AND revision=?5",
                rusqlite::params![&request.conversation_id, new_revision, &request.operation_id, request.now_ms, revision],
            )
            .map_err(|error| sqlite_error("close Conversation admission guard failed", error))?;
        transaction
            .execute(
                "UPDATE submission_admissions SET admission_state='closed', claim_owner=NULL, claim_token=NULL, claim_expires_at_ms=NULL, updated_at_ms=?2 WHERE conversation_id=?1 AND handoff_state='pending'",
                rusqlite::params![&request.conversation_id, request.now_ms],
            )
            .map_err(|error| sqlite_error("fence pending submission admissions failed", error))?;
        if let Some(task_id) = active_task_id {
            let should_cancel = transaction
                .query_row(
                    "SELECT 1 FROM submission_admissions WHERE task_id=?1 AND handoff_state='pending'",
                    rusqlite::params![&task_id],
                    |_| Ok(()),
                )
                .optional()
                .map_err(|error| sqlite_error("check pending admitted Task failed", error))?
                .is_some();
            if should_cancel
                && let Some(existing) = task_record_by_id(&transaction, &task_id)?
                && existing.status == "accepted"
            {
                let mut cancelling = existing.clone();
                cancelling.status = "cancelling".to_owned();
                cancelling.cancel_requested_at = Some(request.now_ms.to_string());
                cancelling.updated_at = Some(request.now_ms.to_string());
                validate_task_update(&existing, &cancelling)?;
                let mut cancelled = cancelling.clone();
                cancelled.status = "cancelled".to_owned();
                validate_task_update(&cancelling, &cancelled)?;
                upsert_task_record(&transaction, &cancelled)?;
            }
        }
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit Conversation admission close failed", error))?;
        Ok(CloseConversationAdmissionResponse {
            disposition: ConversationAdmissionCloseDisposition::Closed,
            revision: new_revision as u64,
            error: None,
        })
    }

    pub fn reserve_message_identity(
        &self,
        request: ReserveMessageIdentityRequest,
    ) -> Result<ReserveMessageIdentityResponse, RuntimeSidecarError> {
        validate_message_identity(&request.identity, false)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|error| sqlite_error("begin Message identity reservation failed", error))?;
        require_finalized_submission_authority(&transaction)?;
        if let Some(existing) = message_identity_by_id(&transaction, &request.identity.message_id)?
        {
            let exact = message_identity_is_exact_replay(&existing, &request.identity);
            transaction.commit().map_err(|error| {
                sqlite_error("commit Message identity reservation replay failed", error)
            })?;
            return Ok(ReserveMessageIdentityResponse {
                disposition: if exact {
                    MessageIdentityDisposition::ExactReplay
                } else {
                    MessageIdentityDisposition::Conflict
                },
                identity: exact.then_some(existing),
                error: None,
            });
        }
        transaction
            .execute(
                "INSERT OR IGNORE INTO submission_conversations(conversation_id, username, status, revision, active_task_id, close_operation_id, updated_at_ms) VALUES (?1, ?2, 'active', 1, NULL, NULL, ?3)",
                rusqlite::params![&request.identity.conversation_id, &request.identity.username, request.identity.reserved_at_ms],
            )
            .map_err(|error| sqlite_error("create Message identity Conversation guard failed", error))?;
        let (username, status): (String, String) = transaction
            .query_row(
                "SELECT username, status FROM submission_conversations WHERE conversation_id=?1",
                rusqlite::params![&request.identity.conversation_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map_err(|error| {
                sqlite_error("read Message identity Conversation guard failed", error)
            })?;
        if username != request.identity.username || status != "active" {
            return Ok(ReserveMessageIdentityResponse {
                disposition: MessageIdentityDisposition::ConversationNotAvailable,
                identity: None,
                error: None,
            });
        }
        insert_message_identity(&transaction, &request.identity)?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit Message identity reservation failed", error))?;
        Ok(ReserveMessageIdentityResponse {
            disposition: MessageIdentityDisposition::Created,
            identity: Some(request.identity),
            error: None,
        })
    }

    fn initialize_schema(&self) -> Result<(), RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        connection
            .execute_batch(
                r"
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS submitted_tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    root_message_id TEXT,
                    status TEXT,
                    routing_mode TEXT,
                    requested_capability_id TEXT,
                    summary TEXT,
                    cancel_requested_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    route_mode TEXT,
                    real_path TEXT,
                    shadow_path TEXT,
                    config_version TEXT,
                    reason_code TEXT,
                    cohort_id TEXT,
                    assignment_key_hash TEXT,
                    assigned_at TEXT
                );
                CREATE TABLE IF NOT EXISTS task_submit_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT,
                    root_message_id TEXT,
                    status TEXT,
                    routing_mode TEXT,
                    requested_capability_id TEXT,
                    summary TEXT,
                    cancel_requested_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    route_mode TEXT,
                    real_path TEXT,
                    shadow_path TEXT,
                    config_version TEXT,
                    reason_code TEXT,
                    cohort_id TEXT,
                    assignment_key_hash TEXT,
                    assigned_at TEXT
                );
                CREATE TABLE IF NOT EXISTS node_statuses (
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (task_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS task_nodes (
                    node_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    node_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_transition_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    node_json TEXT
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    producer_node_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    is_complete INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    producer_node_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    is_complete INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_events (
                    conversation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json BLOB NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (conversation_id, task_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS task_leases (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    renew_token TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lease_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    renew_token TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cancellation_tokens (
                    task_id TEXT PRIMARY KEY,
                    requested_at_ms INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    terminal_policy TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cancellation_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    written INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bundle_pins (
                    task_id TEXT NOT NULL,
                    bundle_kind TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    released_at_ms INTEGER,
                    PRIMARY KEY (task_id, bundle_kind)
                );
                CREATE TABLE IF NOT EXISTS bundle_revision_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    bundle_kind TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    released INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL,
                    claim_token TEXT,
                    run_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_items (
                    item_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    item_json TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS agent_state_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_final_projections (
                    run_id TEXT PRIMARY KEY,
                    projection_json BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submission_authority_meta (
                    singleton_key INTEGER PRIMARY KEY CHECK(singleton_key = 1),
                    state TEXT NOT NULL CHECK(state IN ('uninitialized', 'finalized')),
                    finalization_receipt_sha256 TEXT,
                    finalization_receipt_json BLOB,
                    finalized_at_ms INTEGER,
                    CHECK(
                        (state = 'uninitialized' AND finalization_receipt_sha256 IS NULL AND finalization_receipt_json IS NULL AND finalized_at_ms IS NULL)
                        OR
                        (state = 'finalized' AND length(finalization_receipt_sha256) = 64 AND finalization_receipt_sha256 NOT GLOB '*[^0-9a-f]*' AND finalization_receipt_json IS NOT NULL AND finalized_at_ms >= 0)
                    )
                );
                INSERT OR IGNORE INTO submission_authority_meta(singleton_key, state)
                VALUES (1, 'uninitialized');
                CREATE TABLE IF NOT EXISTS submission_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'unavailable')),
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    active_task_id TEXT UNIQUE,
                    close_operation_id TEXT,
                    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0)
                );
                CREATE TABLE IF NOT EXISTS submission_message_identities (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    identity_kind TEXT NOT NULL CHECK(identity_kind IN ('submission', 'interrupt', 'server_internal', 'file_visible', 'legacy_conflict_only')),
                    role TEXT,
                    message_type TEXT,
                    message_created_at_ms INTEGER,
                    task_id TEXT,
                    request_fingerprint TEXT,
                    reserved_at_ms INTEGER NOT NULL CHECK(reserved_at_ms >= 0),
                    CHECK(message_created_at_ms IS NULL OR message_created_at_ms >= 0),
                    CHECK(request_fingerprint IS NULL OR (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*')),
                    CHECK(
                        (identity_kind IN ('submission', 'interrupt') AND role IS NOT NULL AND message_type IS NOT NULL AND message_created_at_ms IS NOT NULL AND task_id IS NOT NULL AND length(request_fingerprint) = 64)
                        OR
                        (identity_kind = 'server_internal' AND role IS NOT NULL AND message_type IS NOT NULL AND message_created_at_ms IS NOT NULL AND task_id IS NOT NULL AND request_fingerprint IS NULL)
                        OR
                        (identity_kind = 'file_visible' AND role IS NOT NULL AND message_type IS NOT NULL AND message_created_at_ms IS NOT NULL AND request_fingerprint IS NULL)
                        OR
                        (identity_kind = 'legacy_conflict_only' AND request_fingerprint IS NULL)
                    ),
                    FOREIGN KEY(conversation_id) REFERENCES submission_conversations(conversation_id)
                );
                CREATE TABLE IF NOT EXISTS submission_admissions (
                    message_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
                    conversation_projection_json BLOB NOT NULL,
                    message_projection_json BLOB NOT NULL,
                    projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256) = 64 AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
                    continuation_json BLOB NOT NULL,
                    continuation_sha256 TEXT NOT NULL CHECK(length(continuation_sha256) = 64 AND continuation_sha256 NOT GLOB '*[^0-9a-f]*'),
                    admission_state TEXT NOT NULL CHECK(admission_state IN ('open', 'closed')),
                    projection_state TEXT NOT NULL CHECK(projection_state IN ('pending', 'projected')),
                    preparation_state TEXT NOT NULL CHECK(preparation_state IN ('pending', 'prepared')),
                    prepared_execution_json BLOB,
                    prepared_execution_sha256 TEXT,
                    handoff_state TEXT NOT NULL CHECK(handoff_state IN ('pending', 'handed_off')),
                    handoff_kind TEXT,
                    handoff_identity TEXT,
                    claim_owner TEXT,
                    claim_token TEXT,
                    claim_expires_at_ms INTEGER,
                    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0),
                    CHECK(updated_at_ms >= created_at_ms),
                    CHECK(handoff_kind IS NULL OR handoff_kind IN ('agent_run', 'interrupt', 'no_server_intent')),
                    CHECK(prepared_execution_sha256 IS NULL OR (length(prepared_execution_sha256) = 64 AND prepared_execution_sha256 NOT GLOB '*[^0-9a-f]*')),
                    CHECK(
                        (preparation_state = 'pending' AND prepared_execution_json IS NULL AND prepared_execution_sha256 IS NULL)
                        OR
                        (preparation_state = 'prepared' AND prepared_execution_json IS NOT NULL AND length(prepared_execution_sha256) = 64)
                    ),
                    CHECK(
                        (handoff_state = 'pending' AND handoff_kind IS NULL AND handoff_identity IS NULL)
                        OR
                        (handoff_state = 'handed_off' AND handoff_kind IS NOT NULL AND handoff_identity IS NOT NULL)
                    ),
                    CHECK(
                        (claim_owner IS NULL AND claim_token IS NULL AND claim_expires_at_ms IS NULL)
                        OR
                        (claim_owner IS NOT NULL AND length(claim_owner) > 0 AND length(claim_token) = 64 AND claim_token NOT GLOB '*[^0-9a-f]*' AND claim_expires_at_ms >= 0)
                    ),
                    FOREIGN KEY(message_id) REFERENCES submission_message_identities(message_id),
                    FOREIGN KEY(task_id) REFERENCES submitted_tasks(task_id),
                    FOREIGN KEY(conversation_id) REFERENCES submission_conversations(conversation_id)
                );
                CREATE INDEX IF NOT EXISTS submission_admissions_pending_idx
                ON submission_admissions(admission_state, handoff_state, created_at_ms, message_id);
                CREATE INDEX IF NOT EXISTS submission_message_identities_conversation_idx
                ON submission_message_identities(conversation_id, message_id);
                ",
            )
            .map_err(|error| {
                sqlite_error("initialize runtime sidecar SQLite schema failed", error)
            })?;
        ensure_task_authority_columns(&connection, "submitted_tasks")?;
        ensure_task_authority_columns(&connection, "task_submit_idempotency")?;
        ensure_optional_column(
            &connection,
            "node_transition_idempotency",
            "node_json",
            "TEXT",
        )?;
        verify_submission_authority_schema(&connection)?;
        Ok(())
    }

    fn lock_connection(&self) -> Result<MutexGuard<'_, Connection>, RuntimeSidecarError> {
        self.connection.lock().map_err(|_| {
            RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreUnavailable,
                "runtime sidecar SQLite adapter lock is poisoned",
            )
        })
    }
}

fn verify_submission_authority_schema(connection: &Connection) -> Result<(), RuntimeSidecarError> {
    for (name, expected_sha256) in [
        (
            "submission_authority_meta",
            "a7311e68510f7e982f6010eb7093a04f7a7a76feac8b4fd4939fb5c8154eb52f",
        ),
        (
            "submission_conversations",
            "ee7fb9f658280cda0cfc9d757704d44e3bee5dc5f996a90ea82c8a3b39811a0b",
        ),
        (
            "submission_message_identities",
            "0aeed21ac9fdb8cfe599e4eccb32fb41f52c60ddd996d12353d09e7ab341effd",
        ),
        (
            "submission_admissions",
            "dbb94f60f848bb48fe20306788071ec6183116a7a51657dae871d3cb73a253d8",
        ),
        (
            "submission_admissions_pending_idx",
            "e73752709bcf8267e0f9b4aaf0ac3f8366015c3add13105917f808f979396a34",
        ),
        (
            "submission_message_identities_conversation_idx",
            "e87625657d6e018b7f0dae2d59fbb8f50bfe356686757d091d27b21adab91b8d",
        ),
    ] {
        verify_normalized_schema_sql(connection, name, expected_sha256)?;
    }
    verify_submission_table_info(connection)?;
    verify_submission_foreign_keys(connection)?;
    verify_submission_index_and_trigger_lists(connection)?;
    for (table, expected) in [
        (
            "submission_authority_meta",
            &[
                "singleton_key",
                "state",
                "finalization_receipt_sha256",
                "finalization_receipt_json",
                "finalized_at_ms",
            ][..],
        ),
        (
            "submission_conversations",
            &[
                "conversation_id",
                "username",
                "status",
                "revision",
                "active_task_id",
                "close_operation_id",
                "updated_at_ms",
            ],
        ),
        (
            "submission_message_identities",
            &[
                "message_id",
                "conversation_id",
                "username",
                "identity_kind",
                "role",
                "message_type",
                "message_created_at_ms",
                "task_id",
                "request_fingerprint",
                "reserved_at_ms",
            ],
        ),
        (
            "submission_admissions",
            &[
                "message_id",
                "task_id",
                "conversation_id",
                "username",
                "idempotency_key",
                "request_fingerprint",
                "conversation_projection_json",
                "message_projection_json",
                "projection_sha256",
                "continuation_json",
                "continuation_sha256",
                "admission_state",
                "projection_state",
                "preparation_state",
                "prepared_execution_json",
                "prepared_execution_sha256",
                "handoff_state",
                "handoff_kind",
                "handoff_identity",
                "claim_owner",
                "claim_token",
                "claim_expires_at_ms",
                "created_at_ms",
                "updated_at_ms",
            ],
        ),
    ] {
        let actual = schema_column_names(connection, table)?;
        if actual != expected {
            return Err(migration_blocked(
                "submission authority SQLite table manifest is incompatible",
            ));
        }
    }
    require_schema_sql_fragments(
        connection,
        "submission_authority_meta",
        &[
            "check(singleton_key = 1)",
            "state in ('uninitialized', 'finalized')",
            "finalization_receipt_sha256 not glob '*[^0-9a-f]*'",
            "finalized_at_ms >= 0",
            "state = 'uninitialized' and finalization_receipt_sha256 is null and finalization_receipt_json is null and finalized_at_ms is null",
        ],
    )?;
    require_schema_sql_fragments(
        connection,
        "submission_conversations",
        &[
            "conversation_id text primary key",
            "username text not null",
            "status text not null check(status in ('active', 'unavailable'))",
            "revision integer not null check(revision >= 1)",
            "active_task_id text unique",
            "updated_at_ms integer not null check(updated_at_ms >= 0)",
        ],
    )?;
    require_schema_sql_fragments(
        connection,
        "submission_message_identities",
        &[
            "identity_kind in ('submission', 'interrupt', 'server_internal', 'file_visible', 'legacy_conflict_only')",
            "message_created_at_ms is null or message_created_at_ms >= 0",
            "request_fingerprint not glob '*[^0-9a-f]*'",
            "reserved_at_ms integer not null check(reserved_at_ms >= 0)",
            "identity_kind in ('submission', 'interrupt') and role is not null and message_type is not null and message_created_at_ms is not null and task_id is not null and length(request_fingerprint) = 64",
            "foreign key(conversation_id) references submission_conversations(conversation_id)",
        ],
    )?;
    require_schema_sql_fragments(
        connection,
        "submission_admissions",
        &[
            "projection_sha256 not glob '*[^0-9a-f]*'",
            "continuation_sha256 not glob '*[^0-9a-f]*'",
            "prepared_execution_sha256 not glob '*[^0-9a-f]*'",
            "updated_at_ms >= created_at_ms",
            "admission_state text not null check(admission_state in ('open', 'closed'))",
            "projection_state text not null check(projection_state in ('pending', 'projected'))",
            "preparation_state text not null check(preparation_state in ('pending', 'prepared'))",
            "handoff_state text not null check(handoff_state in ('pending', 'handed_off'))",
            "handoff_kind in ('agent_run', 'interrupt', 'no_server_intent')",
            "claim_token not glob '*[^0-9a-f]*'",
            "preparation_state = 'pending' and prepared_execution_json is null and prepared_execution_sha256 is null",
            "claim_owner is null and claim_token is null and claim_expires_at_ms is null",
            "foreign key(message_id) references submission_message_identities(message_id)",
            "foreign key(task_id) references submitted_tasks(task_id)",
            "foreign key(conversation_id) references submission_conversations(conversation_id)",
        ],
    )?;
    for (index, expected_sql) in [
        (
            "submission_admissions_pending_idx",
            "create index submission_admissions_pending_idx on submission_admissions(admission_state, handoff_state, created_at_ms, message_id)",
        ),
        (
            "submission_message_identities_conversation_idx",
            "create index submission_message_identities_conversation_idx on submission_message_identities(conversation_id, message_id)",
        ),
    ] {
        let sql = connection
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?1",
                rusqlite::params![index],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| sqlite_error("inspect submission authority index failed", error))?;
        let normalized = sql.map(|value| {
            value
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .to_ascii_lowercase()
        });
        if normalized.as_deref() != Some(expected_sql) {
            return Err(migration_blocked(
                "submission authority SQLite index manifest is incompatible",
            ));
        }
    }
    Ok(())
}

fn schema_column_names(
    connection: &Connection,
    table: &str,
) -> Result<Vec<String>, RuntimeSidecarError> {
    let mut statement = connection
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(|error| sqlite_error("inspect submission authority columns failed", error))?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|error| sqlite_error("query submission authority columns failed", error))?;
    rows.map(|row| {
        row.map_err(|error| sqlite_error("read submission authority column failed", error))
    })
    .collect()
}

fn verify_normalized_schema_sql(
    connection: &Connection,
    name: &str,
    expected_sha256: &str,
) -> Result<(), RuntimeSidecarError> {
    let sql = connection
        .query_row(
            "SELECT sql FROM sqlite_master WHERE name=?1 AND sql IS NOT NULL",
            rusqlite::params![name],
            |row| row.get::<_, String>(0),
        )
        .map_err(|error| sqlite_error("read canonical submission schema SQL failed", error))?;
    let normalized = sql
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase();
    if format!("{:x}", Sha256::digest(normalized.as_bytes())) != expected_sha256 {
        return Err(migration_blocked(
            "submission authority normalized SQLite schema is incompatible",
        ));
    }
    Ok(())
}

fn verify_submission_table_info(connection: &Connection) -> Result<(), RuntimeSidecarError> {
    type Column = (&'static str, &'static str, i64, i64);
    let manifests: [(&str, &[Column]); 4] = [
        (
            "submission_authority_meta",
            &[
                ("singleton_key", "INTEGER", 0, 1),
                ("state", "TEXT", 1, 0),
                ("finalization_receipt_sha256", "TEXT", 0, 0),
                ("finalization_receipt_json", "BLOB", 0, 0),
                ("finalized_at_ms", "INTEGER", 0, 0),
            ],
        ),
        (
            "submission_conversations",
            &[
                ("conversation_id", "TEXT", 0, 1),
                ("username", "TEXT", 1, 0),
                ("status", "TEXT", 1, 0),
                ("revision", "INTEGER", 1, 0),
                ("active_task_id", "TEXT", 0, 0),
                ("close_operation_id", "TEXT", 0, 0),
                ("updated_at_ms", "INTEGER", 1, 0),
            ],
        ),
        (
            "submission_message_identities",
            &[
                ("message_id", "TEXT", 0, 1),
                ("conversation_id", "TEXT", 1, 0),
                ("username", "TEXT", 1, 0),
                ("identity_kind", "TEXT", 1, 0),
                ("role", "TEXT", 0, 0),
                ("message_type", "TEXT", 0, 0),
                ("message_created_at_ms", "INTEGER", 0, 0),
                ("task_id", "TEXT", 0, 0),
                ("request_fingerprint", "TEXT", 0, 0),
                ("reserved_at_ms", "INTEGER", 1, 0),
            ],
        ),
        (
            "submission_admissions",
            &[
                ("message_id", "TEXT", 0, 1),
                ("task_id", "TEXT", 1, 0),
                ("conversation_id", "TEXT", 1, 0),
                ("username", "TEXT", 1, 0),
                ("idempotency_key", "TEXT", 1, 0),
                ("request_fingerprint", "TEXT", 1, 0),
                ("conversation_projection_json", "BLOB", 1, 0),
                ("message_projection_json", "BLOB", 1, 0),
                ("projection_sha256", "TEXT", 1, 0),
                ("continuation_json", "BLOB", 1, 0),
                ("continuation_sha256", "TEXT", 1, 0),
                ("admission_state", "TEXT", 1, 0),
                ("projection_state", "TEXT", 1, 0),
                ("preparation_state", "TEXT", 1, 0),
                ("prepared_execution_json", "BLOB", 0, 0),
                ("prepared_execution_sha256", "TEXT", 0, 0),
                ("handoff_state", "TEXT", 1, 0),
                ("handoff_kind", "TEXT", 0, 0),
                ("handoff_identity", "TEXT", 0, 0),
                ("claim_owner", "TEXT", 0, 0),
                ("claim_token", "TEXT", 0, 0),
                ("claim_expires_at_ms", "INTEGER", 0, 0),
                ("created_at_ms", "INTEGER", 1, 0),
                ("updated_at_ms", "INTEGER", 1, 0),
            ],
        ),
    ];
    for (table, expected) in manifests {
        let mut statement = connection
            .prepare(&format!("PRAGMA table_info({table})"))
            .map_err(|error| sqlite_error("inspect canonical table info failed", error))?;
        let actual = statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(5)?,
                ))
            })
            .map_err(|error| sqlite_error("query canonical table info failed", error))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| sqlite_error("read canonical table info failed", error))?;
        let expected = expected
            .iter()
            .map(|(name, kind, not_null, pk)| {
                ((*name).to_owned(), (*kind).to_owned(), *not_null, *pk)
            })
            .collect::<Vec<_>>();
        if actual != expected {
            return Err(migration_blocked(
                "submission authority table_info manifest is incompatible",
            ));
        }
    }
    Ok(())
}

fn verify_submission_foreign_keys(connection: &Connection) -> Result<(), RuntimeSidecarError> {
    for (table, expected) in [
        (
            "submission_authority_meta",
            Vec::<(String, String, String)>::new(),
        ),
        ("submission_conversations", Vec::new()),
        (
            "submission_message_identities",
            vec![(
                "conversation_id".to_owned(),
                "submission_conversations".to_owned(),
                "conversation_id".to_owned(),
            )],
        ),
        (
            "submission_admissions",
            vec![
                (
                    "conversation_id".to_owned(),
                    "submission_conversations".to_owned(),
                    "conversation_id".to_owned(),
                ),
                (
                    "message_id".to_owned(),
                    "submission_message_identities".to_owned(),
                    "message_id".to_owned(),
                ),
                (
                    "task_id".to_owned(),
                    "submitted_tasks".to_owned(),
                    "task_id".to_owned(),
                ),
            ],
        ),
    ] {
        let mut statement = connection
            .prepare(&format!("PRAGMA foreign_key_list({table})"))
            .map_err(|error| sqlite_error("inspect submission foreign keys failed", error))?;
        let mut actual = statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(4)?,
                ))
            })
            .map_err(|error| sqlite_error("query submission foreign keys failed", error))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| sqlite_error("read submission foreign keys failed", error))?;
        actual.sort();
        if actual != expected {
            return Err(migration_blocked(
                "submission authority foreign-key manifest is incompatible",
            ));
        }
    }
    Ok(())
}

fn verify_submission_index_and_trigger_lists(
    connection: &Connection,
) -> Result<(), RuntimeSidecarError> {
    let mut statement = connection
        .prepare("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN ('submission_conversations','submission_message_identities','submission_admissions') ORDER BY name")
        .map_err(|error| sqlite_error("inspect submission indexes failed", error))?;
    let actual = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|error| sqlite_error("query submission indexes failed", error))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| sqlite_error("read submission indexes failed", error))?;
    let expected = [
        "sqlite_autoindex_submission_admissions_1",
        "sqlite_autoindex_submission_admissions_2",
        "sqlite_autoindex_submission_admissions_3",
        "sqlite_autoindex_submission_conversations_1",
        "sqlite_autoindex_submission_conversations_2",
        "sqlite_autoindex_submission_message_identities_1",
        "submission_admissions_pending_idx",
        "submission_message_identities_conversation_idx",
    ];
    if actual != expected {
        return Err(migration_blocked(
            "submission authority index list is incompatible",
        ));
    }
    let trigger_count: i64 = connection
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('submission_authority_meta','submission_conversations','submission_message_identities','submission_admissions')",
            [],
            |row| row.get(0),
        )
        .map_err(|error| sqlite_error("inspect submission triggers failed", error))?;
    if trigger_count != 0 {
        return Err(migration_blocked(
            "submission authority trigger list is incompatible",
        ));
    }
    Ok(())
}

fn require_schema_sql_fragments(
    connection: &Connection,
    table: &str,
    fragments: &[&str],
) -> Result<(), RuntimeSidecarError> {
    let sql = connection
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?1",
            rusqlite::params![table],
            |row| row.get::<_, String>(0),
        )
        .map_err(|error| sqlite_error("read submission authority table SQL failed", error))?;
    let normalized = sql
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase();
    if fragments.iter().any(|fragment| {
        let fragment = fragment
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_ascii_lowercase();
        !normalized.contains(&fragment)
    }) {
        return Err(migration_blocked(
            "submission authority SQLite constraint manifest is incompatible",
        ));
    }
    Ok(())
}

fn ensure_optional_column(
    connection: &Connection,
    table: &str,
    column: &str,
    kind: &str,
) -> Result<(), RuntimeSidecarError> {
    let exists = connection
        .prepare(&format!("PRAGMA table_info({table})"))
        .and_then(|mut statement| {
            let rows = statement.query_map([], |row| row.get::<_, String>(1))?;
            Ok(rows.filter_map(Result::ok).any(|name| name == column))
        })
        .map_err(|error| sqlite_error("inspect additive schema column failed", error))?;
    if !exists {
        connection
            .execute(
                &format!("ALTER TABLE {table} ADD COLUMN {column} {kind}"),
                [],
            )
            .map_err(|error| sqlite_error("add additive schema column failed", error))?;
    }
    Ok(())
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn validate_finalization_receipt(
    digest: &str,
    receipt_json: &[u8],
    finalized_at_ms: i64,
) -> Result<(), RuntimeSidecarError> {
    if !is_lower_sha256(digest) || finalized_at_ms < 0 || receipt_json.len() > 64 * 1024 {
        return Err(write_failed(
            "submission authority finalization receipt is invalid",
        ));
    }
    let value: serde_json::Value = serde_json::from_slice(receipt_json)
        .map_err(|_| write_failed("submission authority finalization receipt JSON is invalid"))?;
    let object = value.as_object().ok_or_else(|| {
        write_failed("submission authority finalization receipt must be an object")
    })?;
    let expected = [
        "schema",
        "result",
        "finalization_receipt_sha256",
        "finalized_at_ms",
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "destination_schema_sha256",
        "inventories",
    ]
    .into_iter()
    .collect::<std::collections::BTreeSet<_>>();
    let actual = object
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    if actual != expected
        || object.get("schema").and_then(serde_json::Value::as_str)
            != Some("maf.submission_authority.import_receipt.v1")
        || object.get("result").and_then(serde_json::Value::as_str) != Some("finalized")
        || object
            .get("finalization_receipt_sha256")
            .and_then(serde_json::Value::as_str)
            != Some(digest)
        || object
            .get("finalized_at_ms")
            .and_then(serde_json::Value::as_i64)
            != Some(finalized_at_ms)
    {
        return Err(write_failed(
            "submission authority finalization receipt fields are invalid",
        ));
    }
    for key in [
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "destination_schema_sha256",
    ] {
        if object
            .get(key)
            .and_then(serde_json::Value::as_str)
            .is_none_or(|value| !is_lower_sha256(value))
        {
            return Err(write_failed(
                "submission authority finalization receipt digest is invalid",
            ));
        }
    }
    let inventories = object
        .get("inventories")
        .and_then(serde_json::Value::as_object)
        .ok_or_else(|| write_failed("submission authority receipt inventories are invalid"))?;
    let inventory_keys = inventories
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    if inventory_keys
        != ["conversations", "message_identities", "active_tasks"]
            .into_iter()
            .collect()
    {
        return Err(write_failed(
            "submission authority receipt inventory fields are invalid",
        ));
    }
    for inventory in inventories.values() {
        let item = inventory
            .as_object()
            .ok_or_else(|| write_failed("submission authority receipt inventory is invalid"))?;
        if item
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>()
            != ["count", "pk_sha256", "canonical_sha256", "finalize_empty"]
                .into_iter()
                .collect()
            || item
                .get("count")
                .and_then(serde_json::Value::as_u64)
                .is_none()
            || item
                .get("pk_sha256")
                .and_then(serde_json::Value::as_str)
                .is_none_or(|value| !is_lower_sha256(value))
            || item
                .get("canonical_sha256")
                .and_then(serde_json::Value::as_str)
                .is_none_or(|value| !is_lower_sha256(value))
            || item
                .get("finalize_empty")
                .and_then(serde_json::Value::as_bool)
                .is_none()
        {
            return Err(write_failed(
                "submission authority receipt inventory shape is invalid",
            ));
        }
    }
    let canonical = serde_json::to_vec(&value)
        .map_err(|_| write_failed("submission authority receipt cannot be canonicalized"))?;
    if canonical != receipt_json {
        return Err(write_failed(
            "submission authority finalization receipt is not canonical",
        ));
    }
    Ok(())
}

fn validate_receipt_inventory_evidence(
    receipt_json: &[u8],
    conversations: &SubmissionInventoryEvidence,
    message_identities: &SubmissionInventoryEvidence,
    active_tasks: &SubmissionInventoryEvidence,
) -> Result<(), RuntimeSidecarError> {
    let value: serde_json::Value = serde_json::from_slice(receipt_json)
        .map_err(|_| write_failed("submission authority finalization receipt JSON is invalid"))?;
    let inventories = value
        .get("inventories")
        .and_then(serde_json::Value::as_object)
        .ok_or_else(|| write_failed("submission authority receipt inventories are invalid"))?;
    for (key, expected) in [
        ("conversations", conversations),
        ("message_identities", message_identities),
        ("active_tasks", active_tasks),
    ] {
        let actual: SubmissionInventoryEvidence =
            serde_json::from_value(inventories.get(key).cloned().ok_or_else(|| {
                write_failed("submission authority receipt inventory is missing")
            })?)
            .map_err(|_| write_failed("submission authority receipt inventory is invalid"))?;
        if &actual != expected {
            return Err(write_failed(
                "submission authority receipt inventory digest mismatch",
            ));
        }
    }
    Ok(())
}

fn validate_receipt_subject_binding(
    receipt_json: &[u8],
    subject_json: &[u8],
) -> Result<(), RuntimeSidecarError> {
    let receipt: serde_json::Value = serde_json::from_slice(receipt_json)
        .map_err(|_| write_failed("submission authority receipt JSON is invalid"))?;
    let subject: serde_json::Value = serde_json::from_slice(subject_json)
        .map_err(|_| write_failed("submission authority subject JSON is invalid"))?;
    if receipt.get("source_identity_sha256") != subject.get("source_identity_sha256")
        || receipt.get("snapshot_boundary_sha256") != subject.get("snapshot_boundary_sha256")
        || receipt.get("writer_fence_sha256") != subject.get("writer_fence_sha256")
        || receipt
            .get("inventories")
            .and_then(|value| value.get("conversations"))
            != subject.get("conversation_inventory")
        || receipt
            .get("inventories")
            .and_then(|value| value.get("message_identities"))
            != subject.get("message_identity_inventory")
        || receipt
            .get("inventories")
            .and_then(|value| value.get("active_tasks"))
            != subject.get("active_task_inventory")
    {
        return Err(write_failed(
            "submission authority receipt is not bound to its finalization subject",
        ));
    }
    Ok(())
}

fn finalized_submission_authority_replay(
    connection: &Connection,
    requested_digest: &str,
) -> Result<Option<SubmissionAuthorityFinalizeResult>, RuntimeSidecarError> {
    let (state, stored_digest, stored_json, stored_at): (
        String,
        Option<String>,
        Option<Vec<u8>>,
        Option<i64>,
    ) = connection
        .query_row(
            "SELECT state, finalization_receipt_sha256, finalization_receipt_json, finalized_at_ms FROM submission_authority_meta WHERE singleton_key=1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .map_err(|error| sqlite_error("read submission authority finalization state failed", error))?;
    if state != "finalized" {
        return Ok(None);
    }
    if stored_digest.as_deref() != Some(requested_digest) {
        return Err(idempotency_conflict(
            "submission authority was finalized with a different receipt",
        ));
    }
    Ok(Some(SubmissionAuthorityFinalizeResult {
        exact_replay: true,
        finalization_receipt_sha256: requested_digest.to_owned(),
        finalization_receipt_json: stored_json
            .ok_or_else(|| write_failed("finalized submission authority receipt is missing"))?,
        finalized_at_ms: stored_at
            .ok_or_else(|| write_failed("finalized submission authority time is missing"))?,
    }))
}

fn empty_inventory_evidence(kind: &str) -> SubmissionInventoryEvidence {
    inventory_evidence(kind, &[], &[]).expect("empty inventory is canonical")
}

fn inventory_evidence(
    kind: &str,
    primary_keys: &[String],
    records: &[serde_json::Value],
) -> Result<SubmissionInventoryEvidence, RuntimeSidecarError> {
    let pk_json = serde_json::to_vec(primary_keys)
        .map_err(|_| write_failed("submission authority inventory PKs cannot be encoded"))?;
    let records_json = serde_json::to_vec(records)
        .map_err(|_| write_failed("submission authority inventory records cannot be encoded"))?;
    let digest = |domain: &str, bytes: &[u8]| {
        let mut hasher = Sha256::new();
        hasher.update(domain.as_bytes());
        hasher.update(b"\0");
        hasher.update(bytes);
        format!("{:x}", hasher.finalize())
    };
    Ok(SubmissionInventoryEvidence {
        count: u32::try_from(records.len())
            .map_err(|_| write_failed("submission authority inventory count exceeds u32"))?,
        pk_sha256: digest(
            &format!("maf.submission_authority.inventory.{kind}.pk.v1"),
            &pk_json,
        ),
        canonical_sha256: digest(
            &format!("maf.submission_authority.inventory.{kind}.records.v1"),
            &records_json,
        ),
        finalize_empty: records.is_empty(),
    })
}

fn conversation_inventory_evidence(
    conversations: &[SubmissionConversationImportRecord],
) -> Result<SubmissionInventoryEvidence, RuntimeSidecarError> {
    let mut records = conversations.to_vec();
    records.sort_by(|left, right| left.conversation_id.cmp(&right.conversation_id));
    let primary_keys = records
        .iter()
        .map(|record| record.conversation_id.clone())
        .collect::<Vec<_>>();
    let canonical = records
        .iter()
        .map(|record| {
            serde_json::json!({
                "active_task_id": record.active_task_id,
                "conversation_id": record.conversation_id,
                "status": record.status,
                "updated_at_ms": record.updated_at_ms,
                "username": record.username,
            })
        })
        .collect::<Vec<_>>();
    inventory_evidence("conversations", &primary_keys, &canonical)
}

fn message_identity_inventory_evidence(
    identities: &[MessageIdentityRecord],
) -> Result<SubmissionInventoryEvidence, RuntimeSidecarError> {
    let mut records = identities.to_vec();
    records.sort_by(|left, right| left.message_id.cmp(&right.message_id));
    let primary_keys = records
        .iter()
        .map(|record| record.message_id.clone())
        .collect::<Vec<_>>();
    let canonical = records
        .iter()
        .map(|record| {
            serde_json::json!({
                "conversation_id": record.conversation_id,
                "identity_kind": identity_kind_name(record.identity_kind),
                "message_created_at_ms": record.message_created_at_ms,
                "message_id": record.message_id,
                "message_type": record.message_type,
                "request_fingerprint": record.request_fingerprint,
                "reserved_at_ms": record.reserved_at_ms,
                "role": record.role,
                "task_id": record.task_id,
                "username": record.username,
            })
        })
        .collect::<Vec<_>>();
    inventory_evidence("message_identities", &primary_keys, &canonical)
}

fn active_task_inventory_evidence(
    connection: &Connection,
) -> Result<SubmissionInventoryEvidence, RuntimeSidecarError> {
    let task_ids = active_task_ids(connection)?;
    let primary_keys = task_ids.iter().cloned().collect::<Vec<_>>();
    let mut records = Vec::with_capacity(primary_keys.len());
    for task_id in &primary_keys {
        let task = task_record_by_id(connection, task_id)?
            .ok_or_else(|| write_failed("active Task inventory row is missing"))?;
        records.push(
            serde_json::to_value(task)
                .map_err(|_| write_failed("active Task inventory cannot be encoded"))?,
        );
    }
    inventory_evidence("active_tasks", &primary_keys, &records)
}

fn validate_active_task_id_inventory(
    active_task_ids: &[String],
    declared: &SubmissionInventoryEvidence,
) -> Result<(), RuntimeSidecarError> {
    let mut primary_keys = active_task_ids.to_vec();
    primary_keys.sort();
    if primary_keys.windows(2).any(|pair| pair[0] == pair[1])
        || primary_keys.iter().any(|value| value.trim().is_empty())
    {
        return Err(write_failed(
            "submission authority active Task ID inventory is invalid",
        ));
    }
    let bytes = serde_json::to_vec(&primary_keys)
        .map_err(|_| write_failed("active Task PK inventory cannot be encoded"))?;
    let mut hasher = Sha256::new();
    hasher.update(b"maf.submission_authority.inventory.active_tasks.pk.v1\0");
    hasher.update(bytes);
    if declared.count != primary_keys.len() as u32
        || declared.finalize_empty != primary_keys.is_empty()
        || declared.pk_sha256 != format!("{:x}", hasher.finalize())
        || !is_lower_sha256(&declared.canonical_sha256)
    {
        return Err(write_failed(
            "submission authority active Task ID inventory digest mismatch",
        ));
    }
    Ok(())
}

fn finalization_subject_inventory(
    subject_json: &[u8],
    key: &str,
) -> Result<SubmissionInventoryEvidence, RuntimeSidecarError> {
    let value: serde_json::Value = serde_json::from_slice(subject_json)
        .map_err(|_| write_failed("submission authority finalization subject JSON is invalid"))?;
    serde_json::from_value(
        value.get(key).cloned().ok_or_else(|| {
            write_failed("submission authority finalization subject is incomplete")
        })?,
    )
    .map_err(|_| write_failed("submission authority finalization inventory is invalid"))
}

fn validate_finalization_subject(
    subject_json: &[u8],
    expected_digest: &str,
    conversations: &SubmissionInventoryEvidence,
    message_identities: &SubmissionInventoryEvidence,
    active_tasks: &SubmissionInventoryEvidence,
) -> Result<(), RuntimeSidecarError> {
    if subject_json.len() > 64 * 1024 {
        return Err(write_failed(
            "submission authority finalization subject exceeds size limit",
        ));
    }
    let value: serde_json::Value = serde_json::from_slice(subject_json)
        .map_err(|_| write_failed("submission authority finalization subject JSON is invalid"))?;
    let object = value.as_object().ok_or_else(|| {
        write_failed("submission authority finalization subject must be an object")
    })?;
    let expected_keys = [
        "schema",
        "source_backend",
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "report_sha256",
        "schema_hash",
        "proto_hash",
        "supported_features_sha256",
        "conversation_inventory",
        "message_identity_inventory",
        "active_task_inventory",
    ]
    .into_iter()
    .collect::<std::collections::BTreeSet<_>>();
    if object
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>()
        != expected_keys
        || object.get("schema").and_then(serde_json::Value::as_str)
            != Some("maf.submission_authority.finalization_subject.v1")
        || object
            .get("source_backend")
            .and_then(serde_json::Value::as_str)
            .is_none_or(|value| !matches!(value, "sqlite" | "postgresql"))
        || object
            .get("schema_hash")
            .and_then(serde_json::Value::as_str)
            != Some(SCHEMA_HASH)
        || object.get("proto_hash").and_then(serde_json::Value::as_str) != Some(PROTO_HASH)
    {
        return Err(write_failed(
            "submission authority finalization subject fields are invalid",
        ));
    }
    for key in [
        "source_identity_sha256",
        "snapshot_boundary_sha256",
        "writer_fence_sha256",
        "report_sha256",
        "supported_features_sha256",
    ] {
        if object
            .get(key)
            .and_then(serde_json::Value::as_str)
            .is_none_or(|value| !is_lower_sha256(value))
        {
            return Err(write_failed(
                "submission authority finalization subject digest is invalid",
            ));
        }
    }
    let supported_features_json = serde_json::to_vec(&crate::supported_features())
        .map_err(|_| write_failed("submission authority features cannot be encoded"))?;
    let supported_features_sha256 = format!("{:x}", Sha256::digest(&supported_features_json));
    if object
        .get("supported_features_sha256")
        .and_then(serde_json::Value::as_str)
        != Some(supported_features_sha256.as_str())
        || finalization_subject_inventory(subject_json, "conversation_inventory")? != *conversations
        || finalization_subject_inventory(subject_json, "message_identity_inventory")?
            != *message_identities
        || finalization_subject_inventory(subject_json, "active_task_inventory")? != *active_tasks
    {
        return Err(write_failed(
            "submission authority finalization subject inventory mismatch",
        ));
    }
    let canonical = serde_json::to_vec(&value)
        .map_err(|_| write_failed("submission authority subject cannot be canonicalized"))?;
    if canonical != subject_json {
        return Err(write_failed(
            "submission authority finalization subject is not canonical",
        ));
    }
    let mut hasher = Sha256::new();
    hasher.update(b"maf.submission_authority.finalization.v1\0");
    hasher.update(subject_json);
    if format!("{:x}", hasher.finalize()) != expected_digest {
        return Err(write_failed(
            "submission authority finalization subject digest mismatch",
        ));
    }
    Ok(())
}

fn validate_import_records(
    request: &SubmissionAuthorityImportRequest,
) -> Result<(), RuntimeSidecarError> {
    if request.conversations.len() > u32::MAX as usize
        || request.message_identities.len() > u32::MAX as usize
        || request.active_task_ids.len() > u32::MAX as usize
    {
        return Err(write_failed(
            "submission authority import count exceeds u32",
        ));
    }
    let mut total_bytes = request
        .finalization_subject_json
        .len()
        .checked_add(request.finalization_receipt_json.len())
        .ok_or_else(|| write_failed("submission authority import size overflows"))?;
    let mut conversation_ids = std::collections::BTreeSet::new();
    let mut conversation_owners = std::collections::BTreeMap::new();
    let mut pointer_ids = std::collections::BTreeSet::new();
    for conversation in &request.conversations {
        let record_bytes = serde_json::to_vec(&serde_json::json!({
            "active_task_id": conversation.active_task_id,
            "conversation_id": conversation.conversation_id,
            "status": conversation.status,
            "updated_at_ms": conversation.updated_at_ms,
            "username": conversation.username,
        }))
        .map_err(|_| write_failed("submission authority Conversation cannot be encoded"))?;
        if record_bytes.len() > 64 * 1024 {
            return Err(write_failed(
                "submission authority import record exceeds 64 KiB",
            ));
        }
        total_bytes = total_bytes
            .checked_add(record_bytes.len())
            .ok_or_else(|| write_failed("submission authority import size overflows"))?;
        if conversation.conversation_id.trim().is_empty()
            || conversation.username.trim().is_empty()
            || !matches!(conversation.status.as_str(), "active" | "unavailable")
            || conversation.updated_at_ms < 0
            || !conversation_ids.insert(conversation.conversation_id.as_str())
            || (conversation.status == "unavailable" && conversation.active_task_id.is_some())
        {
            return Err(write_failed(
                "submission authority import Conversation is invalid",
            ));
        }
        if let Some(task_id) = &conversation.active_task_id
            && !pointer_ids.insert(task_id.as_str())
        {
            return Err(write_failed(
                "submission authority import active Task pointer is duplicated",
            ));
        }
        conversation_owners.insert(
            conversation.conversation_id.as_str(),
            conversation.username.as_str(),
        );
    }
    let requested = request
        .active_task_ids
        .iter()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    if pointer_ids != requested || requested.len() != request.active_task_ids.len() {
        return Err(write_failed(
            "submission authority import active Task pointer inventory mismatch",
        ));
    }
    let mut message_ids = std::collections::BTreeSet::new();
    for identity in &request.message_identities {
        let record_bytes = serde_json::to_vec(&serde_json::json!({
            "conversation_id": identity.conversation_id,
            "identity_kind": identity_kind_name(identity.identity_kind),
            "message_created_at_ms": identity.message_created_at_ms,
            "message_id": identity.message_id,
            "message_type": identity.message_type,
            "request_fingerprint": identity.request_fingerprint,
            "reserved_at_ms": identity.reserved_at_ms,
            "role": identity.role,
            "task_id": identity.task_id,
            "username": identity.username,
        }))
        .map_err(|_| write_failed("submission authority Message identity cannot be encoded"))?;
        if record_bytes.len() > 64 * 1024 {
            return Err(write_failed(
                "submission authority import record exceeds 64 KiB",
            ));
        }
        total_bytes = total_bytes
            .checked_add(record_bytes.len())
            .ok_or_else(|| write_failed("submission authority import size overflows"))?;
        if identity.identity_kind != MessageIdentityKind::LegacyConflictOnly
            || identity.message_id.trim().is_empty()
            || identity.conversation_id.trim().is_empty()
            || identity.username.trim().is_empty()
            || identity.request_fingerprint.is_some()
            || identity.reserved_at_ms < 0
            || !conversation_ids.contains(identity.conversation_id.as_str())
            || conversation_owners
                .get(identity.conversation_id.as_str())
                .copied()
                != Some(identity.username.as_str())
            || !message_ids.insert(identity.message_id.as_str())
        {
            return Err(write_failed(
                "submission authority imported Message identity is invalid",
            ));
        }
    }
    if total_bytes > 1024 * 1024 * 1024 {
        return Err(write_failed("submission authority import exceeds 1 GiB"));
    }
    Ok(())
}

fn active_task_ids(
    connection: &Connection,
) -> Result<std::collections::BTreeSet<String>, RuntimeSidecarError> {
    let mut statement = connection
        .prepare(
            "SELECT task_id FROM submitted_tasks WHERE root_message_id IS NOT NULL AND status IN ('accepted', 'planning', 'running', 'cancelling') ORDER BY task_id",
        )
        .map_err(|error| sqlite_error("prepare active Task inventory failed", error))?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|error| sqlite_error("query active Task inventory failed", error))?;
    rows.map(|row| row.map_err(|error| sqlite_error("read active Task inventory failed", error)))
        .collect()
}

fn require_finalized_submission_authority(
    connection: &Connection,
) -> Result<String, RuntimeSidecarError> {
    let (state, receipt): (String, Option<String>) = connection
        .query_row(
            "SELECT state, finalization_receipt_sha256 FROM submission_authority_meta WHERE singleton_key=1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|error| sqlite_error("read submission authority meta failed", error))?;
    if state != "finalized" {
        return Err(migration_blocked(
            "submission authority must be finalized before online access",
        ));
    }
    receipt.ok_or_else(|| write_failed("finalized submission authority receipt is missing"))
}

fn submission_authority_is_finalized(connection: &Connection) -> Result<bool, RuntimeSidecarError> {
    connection
        .query_row(
            "SELECT state='finalized' FROM submission_authority_meta WHERE singleton_key=1",
            [],
            |row| row.get::<_, bool>(0),
        )
        .map_err(|error| sqlite_error("read submission authority state failed", error))
}

fn submission_or_import_evidence_exists(
    connection: &Connection,
    task_id: &str,
) -> Result<bool, RuntimeSidecarError> {
    let admission = connection
        .query_row(
            "SELECT 1 FROM submission_admissions WHERE task_id=?1",
            rusqlite::params![task_id],
            |_| Ok(()),
        )
        .optional()
        .map_err(|error| sqlite_error("read Task submission evidence failed", error))?
        .is_some();
    if admission {
        return Ok(true);
    }
    connection
        .query_row(
            "SELECT 1 FROM submission_conversations WHERE active_task_id=?1",
            rusqlite::params![task_id],
            |_| Ok(()),
        )
        .optional()
        .map(|value| value.is_some())
        .map_err(|error| sqlite_error("read imported Task evidence failed", error))
}

fn release_submission_guard_for_terminal_task(
    connection: &Connection,
    task: &TaskRecord,
) -> Result<(), RuntimeSidecarError> {
    if matches!(task.status.as_str(), "cancelled" | "completed" | "failed") {
        connection
            .execute(
                "UPDATE submission_conversations SET active_task_id=NULL, revision=revision+1 WHERE conversation_id=?1 AND active_task_id=?2",
                rusqlite::params![&task.conversation_id, &task.task_id],
            )
            .map_err(|error| sqlite_error("release terminal Task admission guard failed", error))?;
    }
    Ok(())
}

fn validate_task_update_with_submission_context(
    connection: &Connection,
    existing: &TaskRecord,
    replacement: &TaskRecord,
) -> Result<(), RuntimeSidecarError> {
    if initial_no_server_terminal_transition_is_allowed(connection, existing, replacement)? {
        return Ok(());
    }
    validate_task_update(existing, replacement)
}

fn initial_no_server_terminal_transition_is_allowed(
    connection: &Connection,
    existing: &TaskRecord,
    replacement: &TaskRecord,
) -> Result<bool, RuntimeSidecarError> {
    if !initial_no_server_task_transition_shape(existing, replacement) {
        return Ok(false);
    }
    let evidence = connection
        .query_row(
            "SELECT continuation_json, prepared_execution_json FROM submission_admissions WHERE task_id=?1 AND admission_state='open' AND preparation_state='prepared' AND handoff_state='pending'",
            rusqlite::params![&existing.task_id],
            |row| Ok((row.get::<_, Vec<u8>>(0)?, row.get::<_, Vec<u8>>(1)?)),
        )
        .optional()
        .map_err(|error| sqlite_error("read initial-no-server preparation evidence failed", error))?;
    let Some((continuation_json, prepared_json)) = evidence else {
        return Ok(false);
    };
    let continuation: serde_json::Value = serde_json::from_slice(&continuation_json)
        .map_err(|_| write_failed("stored continuation JSON is invalid"))?;
    let prepared: serde_json::Value = serde_json::from_slice(&prepared_json)
        .map_err(|_| write_failed("stored prepared execution JSON is invalid"))?;
    Ok(continuation
        .get("initial_no_server_eligible")
        .and_then(serde_json::Value::as_bool)
        == Some(true)
        && prepared
            .get("prepared_kind")
            .and_then(serde_json::Value::as_str)
            == Some("no_server_intent")
        && prepared
            .get("planned_handoff_kind")
            .and_then(serde_json::Value::as_str)
            == Some("no_server_intent"))
}

fn validate_claim_request(
    request: &ClaimPendingSubmissionRequest,
) -> Result<(), RuntimeSidecarError> {
    if request.workflow_owner.trim().is_empty()
        || request.now_ms < 0
        || request.claim_ttl_ms <= 0
        || request.now_ms.checked_add(request.claim_ttl_ms).is_none()
    {
        return Err(write_failed("submission claim timing or owner is invalid"));
    }
    if request.after_created_at_ms.is_some() != request.after_message_id.is_some() {
        return Err(write_failed(
            "submission recovery cursor fields must be all-or-none",
        ));
    }
    Ok(())
}

fn admission_disposition(disposition: SubmissionAdmissionDisposition) -> AdmitSubmissionResponse {
    AdmitSubmissionResponse {
        disposition,
        admission: None,
        claim: None,
        error: None,
    }
}

fn identity_kind_name(kind: MessageIdentityKind) -> &'static str {
    match kind {
        MessageIdentityKind::Submission => "submission",
        MessageIdentityKind::Interrupt => "interrupt",
        MessageIdentityKind::ServerInternal => "server_internal",
        MessageIdentityKind::FileVisible => "file_visible",
        MessageIdentityKind::LegacyConflictOnly => "legacy_conflict_only",
    }
}

fn identity_kind_from_name(value: &str) -> Result<MessageIdentityKind, RuntimeSidecarError> {
    match value {
        "submission" => Ok(MessageIdentityKind::Submission),
        "interrupt" => Ok(MessageIdentityKind::Interrupt),
        "server_internal" => Ok(MessageIdentityKind::ServerInternal),
        "file_visible" => Ok(MessageIdentityKind::FileVisible),
        "legacy_conflict_only" => Ok(MessageIdentityKind::LegacyConflictOnly),
        _ => Err(write_failed("stored Message identity kind is invalid")),
    }
}

fn insert_message_identity(
    connection: &Connection,
    identity: &MessageIdentityRecord,
) -> Result<(), RuntimeSidecarError> {
    connection
        .execute(
            r"INSERT INTO submission_message_identities(
                message_id, conversation_id, username, identity_kind, role, message_type,
                message_created_at_ms, task_id, request_fingerprint, reserved_at_ms
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            rusqlite::params![
                &identity.message_id,
                &identity.conversation_id,
                &identity.username,
                identity_kind_name(identity.identity_kind),
                &identity.role,
                &identity.message_type,
                identity.message_created_at_ms,
                &identity.task_id,
                &identity.request_fingerprint,
                identity.reserved_at_ms,
            ],
        )
        .map(|_| ())
        .map_err(|error| sqlite_error("insert Message identity failed", error))
}

fn message_identity_by_id(
    connection: &Connection,
    message_id: &str,
) -> Result<Option<MessageIdentityRecord>, RuntimeSidecarError> {
    let raw = connection
        .query_row(
            "SELECT message_id, conversation_id, username, identity_kind, role, message_type, message_created_at_ms, task_id, request_fingerprint, reserved_at_ms FROM submission_message_identities WHERE message_id=?1",
            rusqlite::params![message_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, Option<String>>(5)?,
                    row.get::<_, Option<i64>>(6)?,
                    row.get::<_, Option<String>>(7)?,
                    row.get::<_, Option<String>>(8)?,
                    row.get::<_, i64>(9)?,
                ))
            },
        )
        .optional()
        .map_err(|error| sqlite_error("read Message identity failed", error))?;
    raw.map(
        |(
            message_id,
            conversation_id,
            username,
            identity_kind,
            role,
            message_type,
            message_created_at_ms,
            task_id,
            request_fingerprint,
            reserved_at_ms,
        )| {
            Ok(MessageIdentityRecord {
                message_id,
                conversation_id,
                username,
                identity_kind: identity_kind_from_name(&identity_kind)?,
                role,
                message_type,
                message_created_at_ms,
                task_id,
                request_fingerprint,
                reserved_at_ms,
            })
        },
    )
    .transpose()
}

fn projection_state_from_name(
    value: &str,
) -> Result<SubmissionProjectionState, RuntimeSidecarError> {
    match value {
        "pending" => Ok(SubmissionProjectionState::Pending),
        "projected" => Ok(SubmissionProjectionState::Projected),
        _ => Err(write_failed(
            "stored submission projection state is invalid",
        )),
    }
}

fn preparation_state_from_name(
    value: &str,
) -> Result<SubmissionPreparationState, RuntimeSidecarError> {
    match value {
        "pending" => Ok(SubmissionPreparationState::Pending),
        "prepared" => Ok(SubmissionPreparationState::Prepared),
        _ => Err(write_failed(
            "stored submission preparation state is invalid",
        )),
    }
}

fn handoff_state_from_name(value: &str) -> Result<SubmissionHandoffState, RuntimeSidecarError> {
    match value {
        "pending" => Ok(SubmissionHandoffState::Pending),
        "handed_off" => Ok(SubmissionHandoffState::HandedOff),
        _ => Err(write_failed("stored submission handoff state is invalid")),
    }
}

fn admission_by_message_id(
    connection: &Connection,
    message_id: &str,
) -> Result<Option<(SubmissionAdmissionRecord, Option<SubmissionClaim>)>, RuntimeSidecarError> {
    type AdmissionRow = (
        String,
        String,
        String,
        String,
        String,
        String,
        Vec<u8>,
        Vec<u8>,
        String,
        Vec<u8>,
        String,
        String,
        String,
        String,
        Option<Vec<u8>>,
        Option<String>,
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<i64>,
        i64,
        i64,
    );
    let row: Option<AdmissionRow> = connection
        .query_row(
            r"SELECT message_id, task_id, conversation_id, username, idempotency_key,
               request_fingerprint, conversation_projection_json, message_projection_json,
               projection_sha256, continuation_json, continuation_sha256, admission_state,
               projection_state, preparation_state, prepared_execution_json,
               prepared_execution_sha256, handoff_state, handoff_kind, handoff_identity,
               claim_owner, claim_token, claim_expires_at_ms, created_at_ms, updated_at_ms
               FROM submission_admissions WHERE message_id=?1",
            rusqlite::params![message_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                    row.get(7)?,
                    row.get(8)?,
                    row.get(9)?,
                    row.get(10)?,
                    row.get(11)?,
                    row.get(12)?,
                    row.get(13)?,
                    row.get(14)?,
                    row.get(15)?,
                    row.get(16)?,
                    row.get(17)?,
                    row.get(18)?,
                    row.get(19)?,
                    row.get(20)?,
                    row.get(21)?,
                    row.get(22)?,
                    row.get(23)?,
                ))
            },
        )
        .optional()
        .map_err(|error| sqlite_error("read submission admission failed", error))?;
    let Some((
        message_id,
        task_id,
        conversation_id,
        username,
        idempotency_key,
        request_fingerprint,
        conversation_projection_json,
        message_projection_json,
        projection_sha256,
        continuation_json,
        continuation_sha256,
        admission_state,
        projection_state,
        preparation_state,
        prepared_execution_json,
        prepared_execution_sha256,
        handoff_state,
        handoff_kind,
        handoff_identity,
        claim_owner,
        claim_token,
        claim_expires_at_ms,
        created_at_ms,
        updated_at_ms,
    )) = row
    else {
        return Ok(None);
    };
    let task = task_record_by_id(connection, &task_id)?
        .ok_or_else(|| write_failed("submission admission Task is missing"))?;
    let claim = match (claim_owner, claim_token, claim_expires_at_ms) {
        (Some(owner), Some(token), Some(expires_at_ms)) => Some(SubmissionClaim {
            owner,
            token,
            expires_at_ms,
        }),
        (None, None, None) => None,
        _ => return Err(write_failed("stored submission claim shape is invalid")),
    };
    Ok(Some((
        SubmissionAdmissionRecord {
            message_id,
            task_id,
            conversation_id,
            username,
            request_fingerprint,
            conversation_projection_json,
            message_projection_json,
            projection_sha256,
            continuation_json,
            continuation_sha256,
            projection_state: projection_state_from_name(&projection_state)?,
            preparation_state: preparation_state_from_name(&preparation_state)?,
            prepared_execution_json,
            prepared_execution_sha256,
            handoff_state: handoff_state_from_name(&handoff_state)?,
            handoff_kind,
            handoff_identity,
            created_at_ms,
            updated_at_ms,
            closed: admission_state == "closed",
            task,
            idempotency_key,
        },
        claim,
    )))
}

fn submitted_task_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<String>, RuntimeSidecarError> {
    connection
        .query_row(
            "SELECT task_id FROM task_submit_idempotency WHERE idempotency_key = ?1",
            rusqlite::params![idempotency_key],
            |row| row.get(0),
        )
        .optional()
        .map_err(|error| sqlite_error("select task submit idempotency key failed", error))
}

fn submitted_task_conversation_id(
    connection: &Connection,
    task_id: &str,
) -> Result<Option<String>, RuntimeSidecarError> {
    connection
        .query_row(
            "SELECT conversation_id FROM submitted_tasks WHERE task_id = ?1",
            rusqlite::params![task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|error| sqlite_error("select submitted task identity failed", error))
}

const TASK_RECORD_COLUMNS: &str = "task_id, conversation_id, root_message_id, status, routing_mode, requested_capability_id, summary, cancel_requested_at, created_at, updated_at, route_mode, real_path, shadow_path, config_version, reason_code, cohort_id, assignment_key_hash, assigned_at";

fn task_record_by_id(
    connection: &Connection,
    task_id: &str,
) -> Result<Option<TaskRecord>, RuntimeSidecarError> {
    let task = connection
        .query_row(
            &format!("SELECT {TASK_RECORD_COLUMNS} FROM submitted_tasks WHERE task_id = ?1 AND root_message_id IS NOT NULL"),
            rusqlite::params![task_id],
            task_record_from_row,
        )
        .optional()
        .map_err(|error| sqlite_error("select TaskRecord failed", error))?;
    if let Some(task) = &task {
        validate_task_record(task)?;
    }
    Ok(task)
}

fn task_record_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<TaskRecord>, RuntimeSidecarError> {
    let task = connection
        .query_row(
            &format!("SELECT {TASK_RECORD_COLUMNS} FROM task_submit_idempotency WHERE idempotency_key = ?1 AND root_message_id IS NOT NULL"),
            rusqlite::params![idempotency_key],
            task_record_from_row,
        )
        .optional()
        .map_err(|error| sqlite_error("select TaskRecord idempotency snapshot failed", error))?;
    if let Some(task) = &task {
        validate_task_record(task)?;
    }
    Ok(task)
}

fn task_record_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<TaskRecord> {
    let route_mode: Option<String> = row.get(10)?;
    let assignment = match route_mode {
        Some(route_mode) => Some(TaskRouteAssignment {
            route_mode,
            real_path: row.get::<_, Option<String>>(11)?.unwrap_or_default(),
            shadow_path: row.get::<_, Option<String>>(12)?.unwrap_or_default(),
            config_version: row.get::<_, Option<String>>(13)?.unwrap_or_default(),
            reason_code: row.get::<_, Option<String>>(14)?.unwrap_or_default(),
            cohort_id: row.get(15)?,
            assignment_key_hash: row.get(16)?,
            assigned_at: row.get(17)?,
        }),
        None => None,
    };
    Ok(TaskRecord {
        task_id: row.get(0)?,
        conversation_id: row.get(1)?,
        root_message_id: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
        status: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
        routing_mode: row.get::<_, Option<String>>(4)?.unwrap_or_default(),
        requested_capability_id: row.get(5)?,
        summary: row.get(6)?,
        cancel_requested_at: row.get(7)?,
        created_at: row.get(8)?,
        updated_at: row.get(9)?,
        assignment,
    })
}

fn upsert_task_record(
    connection: &Connection,
    task: &TaskRecord,
) -> Result<(), RuntimeSidecarError> {
    let assignment = task.assignment.as_ref();
    connection.execute(
        r"INSERT INTO submitted_tasks (task_id, conversation_id, root_message_id, status, routing_mode, requested_capability_id, summary, cancel_requested_at, created_at, updated_at, route_mode, real_path, shadow_path, config_version, reason_code, cohort_id, assignment_key_hash, assigned_at)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18)
           ON CONFLICT(task_id) DO UPDATE SET root_message_id=COALESCE(submitted_tasks.root_message_id, excluded.root_message_id), status=excluded.status, routing_mode=COALESCE(submitted_tasks.routing_mode, excluded.routing_mode), requested_capability_id=COALESCE(submitted_tasks.requested_capability_id, excluded.requested_capability_id), summary=excluded.summary, cancel_requested_at=excluded.cancel_requested_at, created_at=COALESCE(submitted_tasks.created_at, excluded.created_at), updated_at=excluded.updated_at, route_mode=COALESCE(excluded.route_mode, submitted_tasks.route_mode), real_path=COALESCE(excluded.real_path, submitted_tasks.real_path), shadow_path=COALESCE(excluded.shadow_path, submitted_tasks.shadow_path), config_version=COALESCE(excluded.config_version, submitted_tasks.config_version), reason_code=COALESCE(excluded.reason_code, submitted_tasks.reason_code), cohort_id=COALESCE(excluded.cohort_id, submitted_tasks.cohort_id), assignment_key_hash=COALESCE(excluded.assignment_key_hash, submitted_tasks.assignment_key_hash), assigned_at=COALESCE(excluded.assigned_at, submitted_tasks.assigned_at)",
        rusqlite::params_from_iter(task_values(task, assignment)),
    ).map(|_| ()).map_err(|error| sqlite_error("upsert TaskRecord failed", error))
}

fn agent_items_for_run(
    connection: &Connection,
    run_id: &str,
) -> Result<Vec<AgentItemRecord>, RuntimeSidecarError> {
    let mut statement = connection
        .prepare("SELECT item_json FROM agent_items WHERE run_id = ?1 ORDER BY sequence")
        .map_err(|error| sqlite_error("prepare AgentItem relationship check failed", error))?;
    let rows = statement
        .query_map(rusqlite::params![run_id], |row| row.get::<_, String>(0))
        .map_err(|error| sqlite_error("query AgentItem relationship check failed", error))?;
    rows.map(|row| {
        let payload =
            row.map_err(|error| sqlite_error("read AgentItem relationship failed", error))?;
        serde_json::from_str(&payload)
            .map_err(|_| write_failed("decode AgentItem relationship failed"))
    })
    .collect()
}

fn insert_task_record_idempotency(
    connection: &Connection,
    idempotency_key: &str,
    task: &TaskRecord,
) -> Result<(), RuntimeSidecarError> {
    let assignment = task.assignment.as_ref();
    let mut values = vec![Value::Text(idempotency_key.to_owned())];
    values.extend(task_values(task, assignment));
    connection.execute(
        r"INSERT INTO task_submit_idempotency (idempotency_key, task_id, conversation_id, root_message_id, status, routing_mode, requested_capability_id, summary, cancel_requested_at, created_at, updated_at, route_mode, real_path, shadow_path, config_version, reason_code, cohort_id, assignment_key_hash, assigned_at)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19)",
        rusqlite::params_from_iter(values),
    ).map(|_| ()).map_err(|error| sqlite_error("insert TaskRecord idempotency snapshot failed", error))
}

fn task_values(task: &TaskRecord, assignment: Option<&TaskRouteAssignment>) -> Vec<Value> {
    vec![
        text_value(&task.task_id),
        text_value(&task.conversation_id),
        text_value(&task.root_message_id),
        text_value(&task.status),
        text_value(&task.routing_mode),
        optional_text_value(task.requested_capability_id.as_deref()),
        optional_text_value(task.summary.as_deref()),
        optional_text_value(task.cancel_requested_at.as_deref()),
        optional_text_value(task.created_at.as_deref()),
        optional_text_value(task.updated_at.as_deref()),
        optional_text_value(assignment.map(|value| value.route_mode.as_str())),
        optional_text_value(assignment.map(|value| value.real_path.as_str())),
        optional_text_value(assignment.map(|value| value.shadow_path.as_str())),
        optional_text_value(assignment.map(|value| value.config_version.as_str())),
        optional_text_value(assignment.map(|value| value.reason_code.as_str())),
        optional_text_value(assignment.and_then(|value| value.cohort_id.as_deref())),
        optional_text_value(assignment.and_then(|value| value.assignment_key_hash.as_deref())),
        optional_text_value(assignment.and_then(|value| value.assigned_at.as_deref())),
    ]
}

fn text_value(value: &str) -> Value {
    Value::Text(value.to_owned())
}

fn optional_text_value(value: Option<&str>) -> Value {
    value.map_or(Value::Null, |value| Value::Text(value.to_owned()))
}

fn ensure_task_authority_columns(
    connection: &Connection,
    table: &str,
) -> Result<(), RuntimeSidecarError> {
    let columns = [
        ("conversation_id", "TEXT"),
        ("root_message_id", "TEXT"),
        ("status", "TEXT"),
        ("routing_mode", "TEXT"),
        ("requested_capability_id", "TEXT"),
        ("summary", "TEXT"),
        ("cancel_requested_at", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("route_mode", "TEXT"),
        ("real_path", "TEXT"),
        ("shadow_path", "TEXT"),
        ("config_version", "TEXT"),
        ("reason_code", "TEXT"),
        ("cohort_id", "TEXT"),
        ("assignment_key_hash", "TEXT"),
        ("assigned_at", "TEXT"),
    ];
    for (column, kind) in columns {
        let sql = format!("ALTER TABLE {table} ADD COLUMN {column} {kind}");
        if let Err(error) = connection.execute(&sql, []) {
            let duplicate = error.to_string().contains("duplicate column name");
            if !duplicate {
                return Err(sqlite_error(
                    "additive TaskRecord schema migration failed",
                    error,
                ));
            }
        }
    }
    Ok(())
}

fn node_transition_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<(NodeTransitionResult, Option<TaskNodeRecord>)>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, node_id, status, node_json
            FROM node_transition_idempotency
            WHERE idempotency_key = ?1
            ",
            rusqlite::params![idempotency_key],
            |row| {
                let result = NodeTransitionResult {
                    task_id: row.get(0)?,
                    node_id: row.get(1)?,
                    status: row.get(2)?,
                };
                let node_json: Option<String> = row.get(3)?;
                Ok((result, node_json))
            },
        )
        .optional()
        .map_err(|error| sqlite_error("select node transition idempotency key failed", error))?
        .map(|(result, node_json)| {
            Ok((
                result,
                node_json
                    .as_deref()
                    .map(decode_task_node_json)
                    .transpose()?,
            ))
        })
        .transpose()
}

fn task_node_by_id(
    connection: &Connection,
    node_id: &str,
) -> Result<Option<TaskNodeRecord>, RuntimeSidecarError> {
    let node_json = connection
        .query_row(
            "SELECT node_json FROM task_nodes WHERE node_id = ?1",
            rusqlite::params![node_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| sqlite_error("select TaskNodeRecord failed", error))?;
    node_json.as_deref().map(decode_task_node_json).transpose()
}

fn decode_task_node_json(payload: &str) -> Result<TaskNodeRecord, RuntimeSidecarError> {
    let node: TaskNodeRecord =
        serde_json::from_str(payload).map_err(|_| write_failed("decode TaskNodeRecord failed"))?;
    validate_task_node_record(&node)?;
    Ok(node)
}

fn artifact_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<ArtifactRecord>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT artifact_id, task_id, producer_node_id, artifact_type, storage_ref,
                   summary, is_complete, created_at
            FROM artifact_idempotency
            WHERE idempotency_key = ?1
            ",
            rusqlite::params![idempotency_key],
            artifact_from_row,
        )
        .optional()
        .map_err(|error| sqlite_error("select artifact idempotency key failed", error))
}

fn artifact_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ArtifactRecord> {
    Ok(ArtifactRecord {
        artifact_id: row.get(0)?,
        task_id: row.get(1)?,
        producer_node_id: row.get(2)?,
        artifact_type: row.get(3)?,
        storage_ref: row.get(4)?,
        summary: row.get(5)?,
        is_complete: row.get::<_, i64>(6)? != 0,
        created_at: row.get(7)?,
    })
}

fn event_cursor_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<EventCursor>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT conversation_id, task_id, sequence, created_at_ms
            FROM runtime_events
            WHERE idempotency_key = ?1
            ",
            rusqlite::params![idempotency_key],
            |row| {
                Ok(EventCursor {
                    conversation_id: row.get(0)?,
                    task_id: row.get(1)?,
                    sequence: row.get::<_, i64>(2)? as u64,
                    created_at_ms: row.get(3)?,
                })
            },
        )
        .optional()
        .map_err(|error| sqlite_error("select runtime event idempotency key failed", error))
}

fn lease_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<TaskLease>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, owner_id, revision, expires_at_ms, renew_token
            FROM lease_idempotency
            WHERE idempotency_key = ?1
            ",
            rusqlite::params![idempotency_key],
            lease_from_row,
        )
        .optional()
        .map_err(|error| sqlite_error("select task lease idempotency key failed", error))
}

fn fetch_lease(
    connection: &Connection,
    task_id: &str,
) -> Result<Option<TaskLease>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, owner_id, revision, expires_at_ms, renew_token
            FROM task_leases
            WHERE task_id = ?1
            ",
            rusqlite::params![task_id],
            lease_from_row,
        )
        .optional()
        .map_err(|error| sqlite_error("select task lease failed", error))
}

fn lease_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<TaskLease> {
    Ok(TaskLease {
        task_id: row.get(0)?,
        owner_id: row.get(1)?,
        revision: row.get::<_, i64>(2)? as u64,
        expires_at_ms: row.get(3)?,
        renew_token: row.get(4)?,
    })
}

fn upsert_lease(connection: &Connection, lease: &TaskLease) -> Result<(), RuntimeSidecarError> {
    connection
        .execute(
            r"
            INSERT INTO task_leases (
                task_id,
                owner_id,
                revision,
                expires_at_ms,
                renew_token
            )
            VALUES (?1, ?2, ?3, ?4, ?5)
            ON CONFLICT(task_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                revision = excluded.revision,
                expires_at_ms = excluded.expires_at_ms,
                renew_token = excluded.renew_token
            ",
            rusqlite::params![
                &lease.task_id,
                &lease.owner_id,
                lease.revision as i64,
                lease.expires_at_ms,
                &lease.renew_token
            ],
        )
        .map(|_| ())
        .map_err(|error| sqlite_error("upsert task lease failed", error))
}

fn cancellation_written_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<bool>, RuntimeSidecarError> {
    connection
        .query_row(
            "SELECT written FROM cancellation_idempotency WHERE idempotency_key = ?1",
            rusqlite::params![idempotency_key],
            |row| Ok(row.get::<_, i64>(0)? != 0),
        )
        .optional()
        .map_err(|error| sqlite_error("select cancellation token idempotency key failed", error))
}

fn fetch_cancellation_token(
    connection: &Connection,
    task_id: &str,
) -> Result<Option<CancellationToken>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, requested_at_ms, reason, terminal_policy
            FROM cancellation_tokens
            WHERE task_id = ?1
            ",
            rusqlite::params![task_id],
            |row| {
                Ok(CancellationToken {
                    task_id: row.get(0)?,
                    requested_at_ms: row.get(1)?,
                    reason: row.get(2)?,
                    terminal_policy: row.get(3)?,
                })
            },
        )
        .optional()
        .map_err(|error| sqlite_error("select cancellation token failed", error))
}

fn bundle_revision_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<BundleRevisionResult>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, bundle_kind, revision, released
            FROM bundle_revision_idempotency
            WHERE idempotency_key = ?1
            ",
            rusqlite::params![idempotency_key],
            |row| {
                Ok(BundleRevisionResult {
                    task_id: row.get(0)?,
                    bundle_kind: row.get(1)?,
                    revision: row.get(2)?,
                    released: row.get::<_, i64>(3)? != 0,
                })
            },
        )
        .optional()
        .map_err(|error| sqlite_error("select bundle revision idempotency key failed", error))
}

fn fetch_bundle_pin(
    connection: &Connection,
    task_id: &str,
    bundle_kind: &str,
) -> Result<Option<BundleRevisionResult>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, bundle_kind, revision, released_at_ms
            FROM bundle_pins
            WHERE task_id = ?1 AND bundle_kind = ?2
            ",
            rusqlite::params![task_id, bundle_kind],
            |row| {
                Ok(BundleRevisionResult {
                    task_id: row.get(0)?,
                    bundle_kind: row.get(1)?,
                    revision: row.get(2)?,
                    released: row.get::<_, Option<i64>>(3)?.is_some(),
                })
            },
        )
        .optional()
        .map_err(|error| sqlite_error("select bundle revision pin failed", error))
}

fn insert_bundle_revision_idempotency(
    connection: &Connection,
    idempotency_key: &str,
    task_id: &str,
    bundle_kind: &str,
    revision: &str,
    released: bool,
) -> Result<(), RuntimeSidecarError> {
    connection
        .execute(
            r"
            INSERT INTO bundle_revision_idempotency (
                idempotency_key,
                task_id,
                bundle_kind,
                revision,
                released
            )
            VALUES (?1, ?2, ?3, ?4, ?5)
            ",
            rusqlite::params![
                idempotency_key,
                task_id,
                bundle_kind,
                revision,
                if released { 1 } else { 0 }
            ],
        )
        .map(|_| ())
        .map_err(|error| sqlite_error("insert bundle revision idempotency key failed", error))
}

fn sqlite_error(message: &str, error: rusqlite::Error) -> RuntimeSidecarError {
    let mut sidecar_error =
        RuntimeSidecarError::new(RuntimeSidecarErrorCode::RuntimeStoreWriteFailed, message);
    sidecar_error.safe_metadata.insert(
        "sqlite_error".to_owned(),
        sqlite_error_kind(&error).to_owned(),
    );
    sidecar_error
}

fn sqlite_unavailable(message: &str, error: rusqlite::Error) -> RuntimeSidecarError {
    let mut sidecar_error =
        RuntimeSidecarError::new(RuntimeSidecarErrorCode::RuntimeStoreUnavailable, message);
    sidecar_error.safe_metadata.insert(
        "sqlite_error".to_owned(),
        sqlite_error_kind(&error).to_owned(),
    );
    sidecar_error
}

fn sqlite_error_kind(error: &rusqlite::Error) -> &'static str {
    match error {
        rusqlite::Error::SqliteFailure(_, _) => "sqlite_failure",
        rusqlite::Error::QueryReturnedNoRows => "query_returned_no_rows",
        rusqlite::Error::InvalidColumnIndex(_) => "invalid_column_index",
        rusqlite::Error::InvalidColumnName(_) => "invalid_column_name",
        rusqlite::Error::InvalidColumnType(_, _, _) => "invalid_column_type",
        rusqlite::Error::FromSqlConversionFailure(_, _, _) => "from_sql_conversion_failure",
        rusqlite::Error::IntegralValueOutOfRange(_, _) => "integral_value_out_of_range",
        rusqlite::Error::ToSqlConversionFailure(_) => "to_sql_conversion_failure",
        _ => "sqlite_error",
    }
}
