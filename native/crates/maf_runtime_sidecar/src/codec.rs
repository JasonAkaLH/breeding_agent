use crate::{
    AgentItemRecord, AgentRunRecord, AgentStateResponse, ArtifactRecord, ArtifactResponse,
    BundleRevisionResponse, EventCursor, HealthState, Idempotency, LeaseResponse,
    ListArtifactsForTaskResponse, ReadinessState, RuntimeSidecarError, RuntimeSidecarVersion,
    TaskNodeRecord, TaskRecord, TaskRouteAssignment, TypedErrorEnvelope, common_pb, runtime_pb,
};

pub(super) fn pb_idempotency_key(idempotency: Option<runtime_pb::Idempotency>) -> String {
    idempotency.map_or_else(String::new, |value| value.key)
}

pub(super) fn missing_features_from_error(error: &RuntimeSidecarError) -> Vec<String> {
    error
        .safe_metadata
        .get("missing_features")
        .map(|features| {
            features
                .split(',')
                .filter(|feature| !feature.is_empty())
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

pub(super) fn version_to_pb(version: RuntimeSidecarVersion) -> common_pb::VersionInfo {
    common_pb::VersionInfo {
        component: version.component,
        build_version: env!("CARGO_PKG_VERSION").to_owned(),
        protocol_version: version.protocol_version,
        schema_hash: version.schema_hash,
        error_code_table_hash: version.error_code_table_hash,
        supported_features: version.supported_features,
        min_client_version: "0.1.0".to_owned(),
        max_client_version: "0.1.x".to_owned(),
    }
}

pub(super) fn health_state_to_pb(state: HealthState) -> common_pb::HealthState {
    match state {
        HealthState::Serving => common_pb::HealthState::Serving,
        HealthState::NotServing => common_pb::HealthState::NotServing,
        HealthState::Degraded => common_pb::HealthState::Degraded,
    }
}

pub(super) fn readiness_state_to_pb(state: ReadinessState) -> common_pb::ReadinessState {
    match state {
        ReadinessState::Ready => common_pb::ReadinessState::Ready,
        ReadinessState::NotReady => common_pb::ReadinessState::NotReady,
    }
}

pub(super) fn typed_error_to_pb(error: TypedErrorEnvelope) -> common_pb::TypedError {
    common_pb::TypedError {
        code: error.code,
        message: error.message,
        retriable: error.retriable,
        category: error_category_to_pb(&error.category) as i32,
        safe_metadata: error.safe_metadata.into_iter().collect(),
    }
}

fn error_category_to_pb(category: &str) -> common_pb::ErrorCategory {
    match category {
        "configuration" => common_pb::ErrorCategory::Configuration,
        "compatibility" => common_pb::ErrorCategory::Compatibility,
        "security" => common_pb::ErrorCategory::Security,
        "resource_limit" => common_pb::ErrorCategory::ResourceLimit,
        "protocol" => common_pb::ErrorCategory::Protocol,
        "upstream" => common_pb::ErrorCategory::Upstream,
        "cancellation" => common_pb::ErrorCategory::Cancellation,
        _ => common_pb::ErrorCategory::Internal,
    }
}

pub(super) fn idempotency_from_pb(idempotency: runtime_pb::Idempotency) -> Idempotency {
    Idempotency {
        key: idempotency.key,
        owner: idempotency.owner,
        deadline_ms: idempotency.deadline_ms,
    }
}

pub(super) fn cursor_to_pb(cursor: EventCursor) -> runtime_pb::EventCursor {
    runtime_pb::EventCursor {
        conversation_id: cursor.conversation_id,
        task_id: cursor.task_id,
        sequence: cursor.sequence,
        created_at_ms: cursor.created_at_ms,
    }
}

fn task_assignment_from_pb(assignment: runtime_pb::TaskRouteAssignment) -> TaskRouteAssignment {
    TaskRouteAssignment {
        route_mode: assignment.route_mode,
        real_path: assignment.real_path,
        shadow_path: assignment.shadow_path,
        config_version: assignment.config_version,
        reason_code: assignment.reason_code,
        cohort_id: assignment.cohort_id,
        assignment_key_hash: assignment.assignment_key_hash,
        assigned_at: assignment.assigned_at,
    }
}

fn task_assignment_to_pb(assignment: TaskRouteAssignment) -> runtime_pb::TaskRouteAssignment {
    runtime_pb::TaskRouteAssignment {
        route_mode: assignment.route_mode,
        real_path: assignment.real_path,
        shadow_path: assignment.shadow_path,
        config_version: assignment.config_version,
        reason_code: assignment.reason_code,
        cohort_id: assignment.cohort_id,
        assignment_key_hash: assignment.assignment_key_hash,
        assigned_at: assignment.assigned_at,
    }
}

pub(super) fn task_record_from_pb(task: runtime_pb::TaskRecord) -> TaskRecord {
    TaskRecord {
        task_id: task.task_id,
        conversation_id: task.conversation_id,
        root_message_id: task.root_message_id,
        status: task.status,
        routing_mode: task.routing_mode,
        requested_capability_id: task.requested_capability_id,
        summary: task.summary,
        cancel_requested_at: task.cancel_requested_at,
        created_at: task.created_at,
        updated_at: task.updated_at,
        assignment: task.assignment.map(task_assignment_from_pb),
    }
}

pub(super) fn task_record_to_pb(task: TaskRecord) -> runtime_pb::TaskRecord {
    runtime_pb::TaskRecord {
        task_id: task.task_id,
        conversation_id: task.conversation_id,
        root_message_id: task.root_message_id,
        status: task.status,
        routing_mode: task.routing_mode,
        requested_capability_id: task.requested_capability_id,
        summary: task.summary,
        cancel_requested_at: task.cancel_requested_at,
        created_at: task.created_at,
        updated_at: task.updated_at,
        assignment: task.assignment.map(task_assignment_to_pb),
    }
}

pub(super) fn agent_run_record_from_pb(run: runtime_pb::AgentRunRecord) -> AgentRunRecord {
    AgentRunRecord {
        run_id: run.run_id,
        task_id: run.task_id,
        conversation_id: run.conversation_id,
        status: run.status,
        model_edition: run.model_edition,
        reasoning_effort: run.reasoning_effort,
        thinking_enabled: run.thinking_enabled,
        binding_option_digests_json: run.binding_option_digests_json,
        next_item_sequence: run.next_item_sequence,
        compacted_through_sequence: run.compacted_through_sequence,
        active_sample_item_id: run.active_sample_item_id,
        waiting_call_item_ids: run.waiting_call_item_ids,
        next_batch_call_ordinal: run.next_batch_call_ordinal,
        claim_owner: run.claim_owner,
        claim_token: run.claim_token,
        lease_expires_at_ms: run.lease_expires_at_ms,
        revision: run.revision,
        terminal_reason_code: run.terminal_reason_code,
        created_at_ms: run.created_at_ms,
        updated_at_ms: run.updated_at_ms,
        terminal_at_ms: run.terminal_at_ms,
    }
}

pub(super) fn agent_run_record_to_pb(run: AgentRunRecord) -> runtime_pb::AgentRunRecord {
    runtime_pb::AgentRunRecord {
        run_id: run.run_id,
        task_id: run.task_id,
        conversation_id: run.conversation_id,
        status: run.status,
        model_edition: run.model_edition,
        reasoning_effort: run.reasoning_effort,
        thinking_enabled: run.thinking_enabled,
        binding_option_digests_json: run.binding_option_digests_json,
        next_item_sequence: run.next_item_sequence,
        compacted_through_sequence: run.compacted_through_sequence,
        active_sample_item_id: run.active_sample_item_id,
        waiting_call_item_ids: run.waiting_call_item_ids,
        next_batch_call_ordinal: run.next_batch_call_ordinal,
        claim_owner: run.claim_owner,
        claim_token: run.claim_token,
        lease_expires_at_ms: run.lease_expires_at_ms,
        revision: run.revision,
        terminal_reason_code: run.terminal_reason_code,
        created_at_ms: run.created_at_ms,
        updated_at_ms: run.updated_at_ms,
        terminal_at_ms: run.terminal_at_ms,
    }
}

pub(super) fn agent_item_record_from_pb(item: runtime_pb::AgentItemRecord) -> AgentItemRecord {
    AgentItemRecord {
        item_id: item.item_id,
        run_id: item.run_id,
        task_id: item.task_id,
        sequence: item.sequence,
        kind: item.kind,
        state: item.state,
        payload_json: item.payload_json,
        payload_size_bytes: item.payload_size_bytes,
        payload_sha256: item.payload_sha256,
        parent_item_id: item.parent_item_id,
        source_call_item_id: item.source_call_item_id,
        provider_sample_id: item.provider_sample_id,
        call_ordinal: item.call_ordinal,
        created_at_ms: item.created_at_ms,
        committed_at_ms: item.committed_at_ms,
    }
}

pub(super) fn agent_item_record_to_pb(item: AgentItemRecord) -> runtime_pb::AgentItemRecord {
    runtime_pb::AgentItemRecord {
        item_id: item.item_id,
        run_id: item.run_id,
        task_id: item.task_id,
        sequence: item.sequence,
        kind: item.kind,
        state: item.state,
        payload_json: item.payload_json,
        payload_size_bytes: item.payload_size_bytes,
        payload_sha256: item.payload_sha256,
        parent_item_id: item.parent_item_id,
        source_call_item_id: item.source_call_item_id,
        provider_sample_id: item.provider_sample_id,
        call_ordinal: item.call_ordinal,
        created_at_ms: item.created_at_ms,
        committed_at_ms: item.committed_at_ms,
    }
}

pub(super) fn agent_state_response_to_pb(
    response: AgentStateResponse,
) -> runtime_pb::AgentStateResponse {
    runtime_pb::AgentStateResponse {
        run: response.run.map(agent_run_record_to_pb),
        items: response
            .items
            .into_iter()
            .map(agent_item_record_to_pb)
            .collect(),
        duplicate: response.duplicate,
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn task_node_record_from_pb(node: runtime_pb::TaskNodeRecord) -> TaskNodeRecord {
    TaskNodeRecord {
        node_id: node.node_id,
        task_id: node.task_id,
        capability_id: node.capability_id,
        assigned_instance_id: node.assigned_instance_id,
        status: node.status,
        input_refs: node.input_refs,
        output_refs: node.output_refs,
        started_at: node.started_at,
        finished_at: node.finished_at,
    }
}

pub(super) fn task_node_record_to_pb(node: TaskNodeRecord) -> runtime_pb::TaskNodeRecord {
    runtime_pb::TaskNodeRecord {
        node_id: node.node_id,
        task_id: node.task_id,
        capability_id: node.capability_id,
        assigned_instance_id: node.assigned_instance_id,
        status: node.status,
        input_refs: node.input_refs,
        output_refs: node.output_refs,
        started_at: node.started_at,
        finished_at: node.finished_at,
    }
}

pub(super) fn artifact_from_pb(artifact: runtime_pb::ArtifactRecord) -> ArtifactRecord {
    ArtifactRecord {
        artifact_id: artifact.artifact_id,
        task_id: artifact.task_id,
        producer_node_id: artifact.producer_node_id,
        artifact_type: artifact.artifact_type,
        storage_ref: artifact.storage_ref,
        summary: artifact.summary,
        is_complete: artifact.is_complete,
        created_at: artifact.created_at,
    }
}

pub(super) fn artifact_to_pb(artifact: ArtifactRecord) -> runtime_pb::ArtifactRecord {
    runtime_pb::ArtifactRecord {
        artifact_id: artifact.artifact_id,
        task_id: artifact.task_id,
        producer_node_id: artifact.producer_node_id,
        artifact_type: artifact.artifact_type,
        storage_ref: artifact.storage_ref,
        summary: artifact.summary,
        is_complete: artifact.is_complete,
        created_at: artifact.created_at,
    }
}

pub(super) fn artifact_response_to_pb(response: ArtifactResponse) -> runtime_pb::ArtifactResponse {
    runtime_pb::ArtifactResponse {
        artifact: response.artifact.map(artifact_to_pb),
        found: response.found,
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn list_artifacts_response_to_pb(
    response: ListArtifactsForTaskResponse,
) -> runtime_pb::ListArtifactsForTaskResponse {
    runtime_pb::ListArtifactsForTaskResponse {
        artifacts: response.artifacts.into_iter().map(artifact_to_pb).collect(),
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn lease_response_to_pb(response: LeaseResponse) -> runtime_pb::LeaseResponse {
    let lease = response.lease;
    runtime_pb::LeaseResponse {
        task_id: lease
            .as_ref()
            .map(|lease| lease.task_id.clone())
            .unwrap_or_default(),
        owner_id: lease
            .as_ref()
            .map(|lease| lease.owner_id.clone())
            .unwrap_or_default(),
        revision: lease.as_ref().map_or(0, |lease| lease.revision),
        expires_at_ms: lease.as_ref().map_or(0, |lease| lease.expires_at_ms),
        renew_token: lease
            .as_ref()
            .map(|lease| lease.renew_token.clone())
            .unwrap_or_default(),
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn bundle_revision_response_to_pb(
    response: BundleRevisionResponse,
) -> runtime_pb::BundleRevisionResponse {
    let result = response.result;
    runtime_pb::BundleRevisionResponse {
        task_id: result
            .as_ref()
            .map(|result| result.task_id.clone())
            .unwrap_or_default(),
        bundle_kind: result
            .as_ref()
            .map(|result| result.bundle_kind.clone())
            .unwrap_or_default(),
        revision: result
            .as_ref()
            .map(|result| result.revision.clone())
            .unwrap_or_default(),
        released: result.as_ref().is_some_and(|result| result.released),
        error: response.error.map(typed_error_to_pb),
    }
}
