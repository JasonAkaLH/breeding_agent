use crate::{
    ArtifactRecord, BundleRevisionResult, CancellationToken, EventCursor, NodeTransitionResult,
    TaskEdgeRecord, require_idempotency_key,
};
use maf_runtime_store::{RuntimeSidecarError, RuntimeSidecarErrorCode, TaskLease};
use maf_task_dispatcher::TaskSubmitResult;
use rusqlite::{Connection, OptionalExtension};
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

    pub fn transition_node(
        &self,
        task_id: &str,
        node_id: &str,
        to_status: &str,
        idempotency_key: &str,
    ) -> Result<NodeTransitionResult, RuntimeSidecarError> {
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection
            .transaction()
            .map_err(|error| sqlite_error("begin node transition transaction failed", error))?;
        if let Some(result) = node_transition_for_idempotency(&transaction, &idempotency_key)? {
            transaction
                .commit()
                .map_err(|error| sqlite_error("commit idempotent node transition failed", error))?;
            return Ok(result);
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
                    status
                )
                VALUES (?1, ?2, ?3, ?4)
                ",
                rusqlite::params![&idempotency_key, task_id, node_id, to_status],
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
                    conversation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_submit_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_statuses (
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (task_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS node_transition_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL
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
            .map_err(|error| sqlite_error("initialize runtime sidecar SQLite schema failed", error))
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

fn node_transition_for_idempotency(
    connection: &Connection,
    idempotency_key: &str,
) -> Result<Option<NodeTransitionResult>, RuntimeSidecarError> {
    connection
        .query_row(
            r"
            SELECT task_id, node_id, status
            FROM node_transition_idempotency
            WHERE idempotency_key = ?1
            ",
            rusqlite::params![idempotency_key],
            |row| {
                Ok(NodeTransitionResult {
                    task_id: row.get(0)?,
                    node_id: row.get(1)?,
                    status: row.get(2)?,
                })
            },
        )
        .optional()
        .map_err(|error| sqlite_error("select node transition idempotency key failed", error))
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
