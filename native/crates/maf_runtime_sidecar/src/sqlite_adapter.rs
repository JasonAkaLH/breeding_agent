use crate::{
    ArtifactRecord, BundleRevisionResult, CancellationToken, EventCursor, NodeTransitionResult,
    TaskEdgeRecord, TaskNodeRecord, TaskRecord, TaskRouteAssignment, idempotency_conflict,
    migration_blocked, require_idempotency_key, validate_task_node_record,
    validate_task_node_update, validate_task_record, validate_task_update,
};
use maf_runtime_store::{RuntimeSidecarError, RuntimeSidecarErrorCode, TaskLease};
use maf_task_dispatcher::TaskSubmitResult;
use rusqlite::{Connection, OptionalExtension, types::Value};
use std::path::Path;
use std::sync::{Mutex, MutexGuard};

#[derive(Debug)]
pub struct RuntimeSidecarSqliteAdapter {
    connection: Mutex<Connection>,
}

impl RuntimeSidecarSqliteAdapter {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, RuntimeSidecarError> {
        let connection = Connection::open(path).map_err(|error| {
            sqlite_unavailable("open runtime sidecar SQLite adapter failed", error)
        })?;
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
        let adapter = Self {
            connection: Mutex::new(connection),
        };
        adapter.initialize_schema()?;
        Ok(adapter)
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
            .transaction()
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
            .transaction()
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
            validate_task_update(&existing, &task)?;
        } else if submitted_task_conversation_id(&transaction, &task.task_id)?.is_some() {
            return Err(migration_blocked(
                "legacy submitted task requires an explicit audited TaskRecord migration",
            ));
        } else {
            validate_expected_status(expected_from_status, None)?;
        }
        upsert_task_record(&transaction, &task)?;
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

    pub fn save_task_edge(
        &self,
        edge: TaskEdgeRecord,
        idempotency_key: &str,
    ) -> Result<TaskEdgeRecord, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin task edge save transaction failed", error))?;
        if let Some(edge) = task_edge_for_idempotency(&transaction, &idempotency_key)? {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent task edge save failed", error))?;
            return Ok(edge);
        }
        transaction
            .execute(
                r"
                INSERT INTO task_edges (
                    task_id,
                    from_node_id,
                    to_node_id,
                    edge_type,
                    condition
                )
                VALUES (?1, ?2, ?3, ?4, ?5)
                ON CONFLICT(task_id, from_node_id, to_node_id) DO UPDATE SET
                    edge_type = excluded.edge_type,
                    condition = excluded.condition
                ",
                rusqlite::params![
                    &edge.task_id,
                    &edge.from_node_id,
                    &edge.to_node_id,
                    &edge.edge_type,
                    &edge.condition,
                ],
            )
            .map_err(|error| sqlite_error("upsert task edge failed", error))?;
        transaction
            .execute(
                r"
                INSERT INTO task_edge_idempotency (
                    idempotency_key,
                    task_id,
                    from_node_id,
                    to_node_id,
                    edge_type,
                    condition
                )
                VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                ",
                rusqlite::params![
                    &idempotency_key,
                    &edge.task_id,
                    &edge.from_node_id,
                    &edge.to_node_id,
                    &edge.edge_type,
                    &edge.condition,
                ],
            )
            .map_err(|error| sqlite_error("insert task edge idempotency key failed", error))?;
        transaction
            .commit()
            .map_err(|error| sqlite_error("commit task edge save failed", error))?;
        Ok(edge)
    }

    pub fn list_task_edges(
        &self,
        task_id: &str,
    ) -> Result<Vec<TaskEdgeRecord>, RuntimeSidecarError> {
        let connection = self.lock_connection()?;
        let mut statement = connection
            .prepare(
                r"
                SELECT task_id, from_node_id, to_node_id, edge_type, condition
                FROM task_edges
                WHERE task_id = ?1
                ORDER BY from_node_id, to_node_id
                ",
            )
            .map_err(|error| sqlite_error("prepare task edge list failed", error))?;
        let rows = statement
            .query_map(rusqlite::params![task_id], task_edge_from_row)
            .map_err(|error| sqlite_error("query task edge list failed", error))?;
        let mut edges = Vec::new();
        for row in rows {
            edges.push(row.map_err(|error| sqlite_error("read task edge row failed", error))?);
        }
        Ok(edges)
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
                    root_node_id TEXT,
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
                    root_node_id TEXT,
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
                CREATE TABLE IF NOT EXISTS task_edges (
                    task_id TEXT NOT NULL,
                    from_node_id TEXT NOT NULL,
                    to_node_id TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    PRIMARY KEY (task_id, from_node_id, to_node_id)
                );
                CREATE TABLE IF NOT EXISTS task_edge_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    from_node_id TEXT NOT NULL,
                    to_node_id TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    condition TEXT NOT NULL
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

fn validate_expected_status(
    expected: Option<&str>,
    current: Option<&String>,
) -> Result<(), RuntimeSidecarError> {
    match (expected, current) {
        (None | Some(""), None) => Ok(()),
        (Some(expected), Some(current)) if expected == current => Ok(()),
        _ => Err(idempotency_conflict(
            "expected status does not match current authoritative status",
        )),
    }
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

const TASK_RECORD_COLUMNS: &str = "task_id, conversation_id, root_message_id, status, routing_mode, requested_capability_id, root_node_id, summary, cancel_requested_at, created_at, updated_at, route_mode, real_path, shadow_path, config_version, reason_code, cohort_id, assignment_key_hash, assigned_at";

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
    let route_mode: Option<String> = row.get(11)?;
    let assignment = match route_mode {
        Some(route_mode) => Some(TaskRouteAssignment {
            route_mode,
            real_path: row.get::<_, Option<String>>(12)?.unwrap_or_default(),
            shadow_path: row.get::<_, Option<String>>(13)?.unwrap_or_default(),
            config_version: row.get::<_, Option<String>>(14)?.unwrap_or_default(),
            reason_code: row.get::<_, Option<String>>(15)?.unwrap_or_default(),
            cohort_id: row.get(16)?,
            assignment_key_hash: row.get(17)?,
            assigned_at: row.get(18)?,
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
        root_node_id: row.get(6)?,
        summary: row.get(7)?,
        cancel_requested_at: row.get(8)?,
        created_at: row.get(9)?,
        updated_at: row.get(10)?,
        assignment,
    })
}

fn upsert_task_record(
    connection: &Connection,
    task: &TaskRecord,
) -> Result<(), RuntimeSidecarError> {
    let assignment = task.assignment.as_ref();
    connection.execute(
        r"INSERT INTO submitted_tasks (task_id, conversation_id, root_message_id, status, routing_mode, requested_capability_id, root_node_id, summary, cancel_requested_at, created_at, updated_at, route_mode, real_path, shadow_path, config_version, reason_code, cohort_id, assignment_key_hash, assigned_at)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19)
           ON CONFLICT(task_id) DO UPDATE SET root_message_id=COALESCE(submitted_tasks.root_message_id, excluded.root_message_id), status=excluded.status, routing_mode=COALESCE(submitted_tasks.routing_mode, excluded.routing_mode), requested_capability_id=COALESCE(submitted_tasks.requested_capability_id, excluded.requested_capability_id), root_node_id=excluded.root_node_id, summary=excluded.summary, cancel_requested_at=excluded.cancel_requested_at, created_at=COALESCE(submitted_tasks.created_at, excluded.created_at), updated_at=excluded.updated_at, route_mode=COALESCE(submitted_tasks.route_mode, excluded.route_mode), real_path=COALESCE(submitted_tasks.real_path, excluded.real_path), shadow_path=COALESCE(submitted_tasks.shadow_path, excluded.shadow_path), config_version=COALESCE(submitted_tasks.config_version, excluded.config_version), reason_code=COALESCE(submitted_tasks.reason_code, excluded.reason_code), cohort_id=COALESCE(submitted_tasks.cohort_id, excluded.cohort_id), assignment_key_hash=COALESCE(submitted_tasks.assignment_key_hash, excluded.assignment_key_hash), assigned_at=COALESCE(submitted_tasks.assigned_at, excluded.assigned_at)",
        rusqlite::params_from_iter(task_values(task, assignment)),
    ).map(|_| ()).map_err(|error| sqlite_error("upsert TaskRecord failed", error))
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
        r"INSERT INTO task_submit_idempotency (idempotency_key, task_id, conversation_id, root_message_id, status, routing_mode, requested_capability_id, root_node_id, summary, cancel_requested_at, created_at, updated_at, route_mode, real_path, shadow_path, config_version, reason_code, cohort_id, assignment_key_hash, assigned_at)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20)",
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
        optional_text_value(task.root_node_id.as_deref()),
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
        ("root_node_id", "TEXT"),
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

fn task_edge_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<TaskEdgeRecord>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, from_node_id, to_node_id, edge_type, condition
            FROM task_edge_idempotency
            WHERE idempotency_key = ?1
            ",
            rusqlite::params![idempotency_key],
            task_edge_from_row,
        )
        .optional()
        .map_err(|error| sqlite_error("select task edge idempotency key failed", error))
}

fn task_edge_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<TaskEdgeRecord> {
    Ok(TaskEdgeRecord {
        task_id: row.get(0)?,
        from_node_id: row.get(1)?,
        to_node_id: row.get(2)?,
        edge_type: row.get(3)?,
        condition: row.get(4)?,
    })
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

fn write_failed(message: &str) -> RuntimeSidecarError {
    RuntimeSidecarError::new(RuntimeSidecarErrorCode::RuntimeStoreWriteFailed, message)
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
