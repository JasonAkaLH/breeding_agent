use crate::{
    AcknowledgeSubmissionHandoffRequest, AcknowledgeSubmissionProjectionRequest,
    AdmitSubmissionRequest, AdmitSubmissionResponse, AgentItemRecord, AgentRunRecord,
    AgentStateResponse, ArtifactRecord, ArtifactResponse, BundleRevisionResponse,
    ClaimPendingSubmissionRequest, ClaimPendingSubmissionResponse,
    CloseConversationAdmissionRequest, CloseConversationAdmissionResponse,
    ConversationAdmissionCloseDisposition, EventCursor, GetSubmissionPreparationRequest,
    GetSubmissionPreparationResponse, HealthState, Idempotency, LeaseResponse,
    ListArtifactsForTaskResponse, MessageIdentityDisposition, MessageIdentityKind,
    MessageIdentityRecord, PrepareSubmissionHandoffRequest, ReadinessState,
    RenewSubmissionClaimRequest, ReserveMessageIdentityRequest, ReserveMessageIdentityResponse,
    RuntimeSidecarError, RuntimeSidecarVersion, SubmissionAdmissionDisposition,
    SubmissionAdmissionRecord, SubmissionAdmissionResponse, SubmissionClaim,
    SubmissionHandoffState, SubmissionPreparationState, SubmissionProjectionState, TaskNodeRecord,
    TaskRecord, TaskRouteAssignment, TypedErrorEnvelope, common_pb, runtime_pb,
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

fn submission_projection_state_from_pb(
    value: i32,
) -> Result<SubmissionProjectionState, RuntimeSidecarError> {
    match runtime_pb::SubmissionProjectionState::try_from(value) {
        Ok(runtime_pb::SubmissionProjectionState::Pending) => {
            Ok(SubmissionProjectionState::Pending)
        }
        Ok(runtime_pb::SubmissionProjectionState::Projected) => {
            Ok(SubmissionProjectionState::Projected)
        }
        _ => Err(crate::write_failed(
            "SubmissionProjectionState is outside the closed contract",
        )),
    }
}

fn submission_preparation_state_from_pb(
    value: i32,
) -> Result<SubmissionPreparationState, RuntimeSidecarError> {
    match runtime_pb::SubmissionPreparationState::try_from(value) {
        Ok(runtime_pb::SubmissionPreparationState::Pending) => {
            Ok(SubmissionPreparationState::Pending)
        }
        Ok(runtime_pb::SubmissionPreparationState::Prepared) => {
            Ok(SubmissionPreparationState::Prepared)
        }
        _ => Err(crate::write_failed(
            "SubmissionPreparationState is outside the closed contract",
        )),
    }
}

fn submission_handoff_state_from_pb(
    value: i32,
) -> Result<SubmissionHandoffState, RuntimeSidecarError> {
    match runtime_pb::SubmissionHandoffState::try_from(value) {
        Ok(runtime_pb::SubmissionHandoffState::Pending) => Ok(SubmissionHandoffState::Pending),
        Ok(runtime_pb::SubmissionHandoffState::HandedOff) => Ok(SubmissionHandoffState::HandedOff),
        _ => Err(crate::write_failed(
            "SubmissionHandoffState is outside the closed contract",
        )),
    }
}

fn submission_projection_state_to_pb(value: SubmissionProjectionState) -> i32 {
    match value {
        SubmissionProjectionState::Pending => runtime_pb::SubmissionProjectionState::Pending as i32,
        SubmissionProjectionState::Projected => {
            runtime_pb::SubmissionProjectionState::Projected as i32
        }
    }
}

fn submission_preparation_state_to_pb(value: SubmissionPreparationState) -> i32 {
    match value {
        SubmissionPreparationState::Pending => {
            runtime_pb::SubmissionPreparationState::Pending as i32
        }
        SubmissionPreparationState::Prepared => {
            runtime_pb::SubmissionPreparationState::Prepared as i32
        }
    }
}

fn submission_handoff_state_to_pb(value: SubmissionHandoffState) -> i32 {
    match value {
        SubmissionHandoffState::Pending => runtime_pb::SubmissionHandoffState::Pending as i32,
        SubmissionHandoffState::HandedOff => runtime_pb::SubmissionHandoffState::HandedOff as i32,
    }
}

fn message_identity_kind_from_pb(value: i32) -> MessageIdentityKind {
    match runtime_pb::MessageIdentityKind::try_from(value) {
        Ok(runtime_pb::MessageIdentityKind::Submission) => MessageIdentityKind::Submission,
        Ok(runtime_pb::MessageIdentityKind::Interrupt) => MessageIdentityKind::Interrupt,
        Ok(runtime_pb::MessageIdentityKind::ServerInternal) => MessageIdentityKind::ServerInternal,
        Ok(runtime_pb::MessageIdentityKind::FileVisible) => MessageIdentityKind::FileVisible,
        Ok(runtime_pb::MessageIdentityKind::LegacyConflictOnly) => {
            MessageIdentityKind::LegacyConflictOnly
        }
        _ => MessageIdentityKind::LegacyConflictOnly,
    }
}

fn message_identity_kind_to_pb(value: MessageIdentityKind) -> i32 {
    match value {
        MessageIdentityKind::Submission => runtime_pb::MessageIdentityKind::Submission as i32,
        MessageIdentityKind::Interrupt => runtime_pb::MessageIdentityKind::Interrupt as i32,
        MessageIdentityKind::ServerInternal => {
            runtime_pb::MessageIdentityKind::ServerInternal as i32
        }
        MessageIdentityKind::FileVisible => runtime_pb::MessageIdentityKind::FileVisible as i32,
        MessageIdentityKind::LegacyConflictOnly => {
            runtime_pb::MessageIdentityKind::LegacyConflictOnly as i32
        }
    }
}

fn submission_claim_to_pb(claim: SubmissionClaim) -> runtime_pb::SubmissionClaim {
    runtime_pb::SubmissionClaim {
        owner: claim.owner,
        token: claim.token,
        expires_at_ms: claim.expires_at_ms,
    }
}

fn message_identity_from_pb(identity: runtime_pb::MessageIdentityRecord) -> MessageIdentityRecord {
    MessageIdentityRecord {
        message_id: identity.message_id,
        conversation_id: identity.conversation_id,
        username: identity.username,
        identity_kind: message_identity_kind_from_pb(identity.identity_kind),
        role: identity.role,
        message_type: identity.message_type,
        message_created_at_ms: identity.message_created_at_ms,
        task_id: identity.task_id,
        request_fingerprint: identity.request_fingerprint,
        reserved_at_ms: identity.reserved_at_ms,
    }
}

fn message_identity_to_pb(identity: MessageIdentityRecord) -> runtime_pb::MessageIdentityRecord {
    runtime_pb::MessageIdentityRecord {
        message_id: identity.message_id,
        conversation_id: identity.conversation_id,
        username: identity.username,
        identity_kind: message_identity_kind_to_pb(identity.identity_kind),
        role: identity.role,
        message_type: identity.message_type,
        message_created_at_ms: identity.message_created_at_ms,
        task_id: identity.task_id,
        request_fingerprint: identity.request_fingerprint,
        reserved_at_ms: identity.reserved_at_ms,
    }
}

fn submission_admission_to_pb(
    admission: SubmissionAdmissionRecord,
) -> runtime_pb::SubmissionAdmissionRecord {
    runtime_pb::SubmissionAdmissionRecord {
        message_id: admission.message_id,
        task_id: admission.task_id,
        conversation_id: admission.conversation_id,
        username: admission.username,
        request_fingerprint: admission.request_fingerprint,
        conversation_projection_json: admission.conversation_projection_json,
        message_projection_json: admission.message_projection_json,
        projection_sha256: admission.projection_sha256,
        continuation_json: admission.continuation_json,
        continuation_sha256: admission.continuation_sha256,
        projection_state: submission_projection_state_to_pb(admission.projection_state),
        preparation_state: submission_preparation_state_to_pb(admission.preparation_state),
        prepared_execution_json: admission.prepared_execution_json,
        prepared_execution_sha256: admission.prepared_execution_sha256,
        handoff_state: submission_handoff_state_to_pb(admission.handoff_state),
        handoff_kind: admission.handoff_kind,
        handoff_identity: admission.handoff_identity,
        created_at_ms: admission.created_at_ms,
        updated_at_ms: admission.updated_at_ms,
        closed: admission.closed,
        task: Some(task_record_to_pb(admission.task)),
        idempotency_key: admission.idempotency_key,
    }
}

pub(super) fn admit_submission_request_from_pb(
    request: runtime_pb::AdmitSubmissionRequest,
) -> Result<AdmitSubmissionRequest, RuntimeSidecarError> {
    let task = request
        .task
        .ok_or_else(|| crate::write_failed("AdmitSubmission TaskRecord is required"))?;
    Ok(AdmitSubmissionRequest {
        message_id: request.message_id,
        task_id: request.task_id,
        conversation_id: request.conversation_id,
        username: request.username,
        request_fingerprint: request.request_fingerprint,
        conversation_projection_json: request.conversation_projection_json,
        message_projection_json: request.message_projection_json,
        projection_sha256: request.projection_sha256,
        continuation_json: request.continuation_json,
        continuation_sha256: request.continuation_sha256,
        message_created_at_ms: request.message_created_at_ms,
        workflow_owner: request.workflow_owner,
        now_ms: request.now_ms,
        claim_ttl_ms: request.claim_ttl_ms,
        task: task_record_from_pb(task),
        idempotency_key: request.idempotency_key,
    })
}

pub(super) fn admit_submission_response_to_pb(
    response: AdmitSubmissionResponse,
) -> runtime_pb::AdmitSubmissionResponse {
    let disposition = match response.disposition {
        SubmissionAdmissionDisposition::Created => {
            runtime_pb::SubmissionAdmissionDisposition::Created
        }
        SubmissionAdmissionDisposition::IdempotentReplay => {
            runtime_pb::SubmissionAdmissionDisposition::IdempotentReplay
        }
        SubmissionAdmissionDisposition::ConversationBusy => {
            runtime_pb::SubmissionAdmissionDisposition::ConversationBusy
        }
        SubmissionAdmissionDisposition::MessageIdConflict => {
            runtime_pb::SubmissionAdmissionDisposition::MessageIdConflict
        }
        SubmissionAdmissionDisposition::ConversationNotAvailable => {
            runtime_pb::SubmissionAdmissionDisposition::ConversationNotAvailable
        }
    };
    runtime_pb::AdmitSubmissionResponse {
        disposition: disposition as i32,
        admission: response.admission.map(submission_admission_to_pb),
        claim: response.claim.map(submission_claim_to_pb),
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn claim_pending_request_from_pb(
    request: runtime_pb::ClaimPendingSubmissionRequest,
) -> ClaimPendingSubmissionRequest {
    ClaimPendingSubmissionRequest {
        workflow_owner: request.workflow_owner,
        now_ms: request.now_ms,
        claim_ttl_ms: request.claim_ttl_ms,
        after_created_at_ms: request.after_created_at_ms,
        after_message_id: request.after_message_id,
    }
}

pub(super) fn claim_pending_response_to_pb(
    response: ClaimPendingSubmissionResponse,
) -> runtime_pb::ClaimPendingSubmissionResponse {
    runtime_pb::ClaimPendingSubmissionResponse {
        found: response.found,
        admission: response.admission.map(submission_admission_to_pb),
        claim: response.claim.map(submission_claim_to_pb),
        authority_state: response.authority_state,
        finalization_receipt_sha256: response.finalization_receipt_sha256,
        error: response.error.map(typed_error_to_pb),
        pending_count: response.pending_count,
        earliest_claim_expires_at_ms: response.earliest_claim_expires_at_ms,
    }
}

pub(super) fn renew_claim_request_from_pb(
    request: runtime_pb::RenewSubmissionClaimRequest,
) -> RenewSubmissionClaimRequest {
    RenewSubmissionClaimRequest {
        message_id: request.message_id,
        workflow_owner: request.workflow_owner,
        claim_token: request.claim_token,
        now_ms: request.now_ms,
        claim_ttl_ms: request.claim_ttl_ms,
    }
}

pub(super) fn projection_ack_request_from_pb(
    request: runtime_pb::AcknowledgeSubmissionProjectionRequest,
) -> Result<AcknowledgeSubmissionProjectionRequest, RuntimeSidecarError> {
    Ok(AcknowledgeSubmissionProjectionRequest {
        message_id: request.message_id,
        workflow_owner: request.workflow_owner,
        claim_token: request.claim_token,
        projection_sha256: request.projection_sha256,
        expected_state: submission_projection_state_from_pb(request.expected_state)?,
        now_ms: request.now_ms,
    })
}

pub(super) fn prepare_handoff_request_from_pb(
    request: runtime_pb::PrepareSubmissionHandoffRequest,
) -> Result<PrepareSubmissionHandoffRequest, RuntimeSidecarError> {
    Ok(PrepareSubmissionHandoffRequest {
        message_id: request.message_id,
        workflow_owner: request.workflow_owner,
        claim_token: request.claim_token,
        prepared_execution_json: request.prepared_execution_json,
        prepared_execution_sha256: request.prepared_execution_sha256,
        expected_state: submission_preparation_state_from_pb(request.expected_state)?,
        now_ms: request.now_ms,
    })
}

pub(super) fn get_preparation_request_from_pb(
    request: runtime_pb::GetSubmissionPreparationRequest,
) -> GetSubmissionPreparationRequest {
    GetSubmissionPreparationRequest {
        username: request.username,
        conversation_id: request.conversation_id,
        task_id: request.task_id,
    }
}

pub(super) fn handoff_ack_request_from_pb(
    request: runtime_pb::AcknowledgeSubmissionHandoffRequest,
) -> Result<AcknowledgeSubmissionHandoffRequest, RuntimeSidecarError> {
    Ok(AcknowledgeSubmissionHandoffRequest {
        message_id: request.message_id,
        workflow_owner: request.workflow_owner,
        claim_token: request.claim_token,
        prepared_execution_sha256: request.prepared_execution_sha256,
        handoff_kind: request.handoff_kind,
        handoff_identity: request.handoff_identity,
        expected_state: submission_handoff_state_from_pb(request.expected_state)?,
        now_ms: request.now_ms,
    })
}

pub(super) fn close_request_from_pb(
    request: runtime_pb::CloseConversationAdmissionRequest,
) -> CloseConversationAdmissionRequest {
    CloseConversationAdmissionRequest {
        username: request.username,
        conversation_id: request.conversation_id,
        operation_id: request.operation_id,
        now_ms: request.now_ms,
    }
}

pub(super) fn reserve_request_from_pb(
    request: runtime_pb::ReserveMessageIdentityRequest,
) -> Result<ReserveMessageIdentityRequest, RuntimeSidecarError> {
    let identity = request
        .identity
        .ok_or_else(|| crate::write_failed("MessageIdentityRecord is required"))?;
    Ok(ReserveMessageIdentityRequest {
        identity: message_identity_from_pb(identity),
    })
}

pub(super) fn submission_claim_response_to_pb(
    result: Result<SubmissionClaim, TypedErrorEnvelope>,
) -> runtime_pb::SubmissionClaimResponse {
    match result {
        Ok(claim) => runtime_pb::SubmissionClaimResponse {
            claim: Some(submission_claim_to_pb(claim)),
            error: None,
        },
        Err(error) => runtime_pb::SubmissionClaimResponse {
            claim: None,
            error: Some(typed_error_to_pb(error)),
        },
    }
}

pub(super) fn submission_admission_response_to_pb(
    response: SubmissionAdmissionResponse,
) -> runtime_pb::SubmissionAdmissionResponse {
    runtime_pb::SubmissionAdmissionResponse {
        admission: response.admission.map(submission_admission_to_pb),
        duplicate: response.duplicate,
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn get_preparation_response_to_pb(
    response: GetSubmissionPreparationResponse,
) -> runtime_pb::GetSubmissionPreparationResponse {
    runtime_pb::GetSubmissionPreparationResponse {
        found: response.found,
        admission: response.admission.map(submission_admission_to_pb),
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn close_response_to_pb(
    response: CloseConversationAdmissionResponse,
) -> runtime_pb::CloseConversationAdmissionResponse {
    let disposition = match response.disposition {
        ConversationAdmissionCloseDisposition::Closed => {
            runtime_pb::ConversationAdmissionCloseDisposition::Closed
        }
        ConversationAdmissionCloseDisposition::ExactReplay => {
            runtime_pb::ConversationAdmissionCloseDisposition::ExactReplay
        }
        ConversationAdmissionCloseDisposition::ConversationNotAvailable => {
            runtime_pb::ConversationAdmissionCloseDisposition::ConversationNotAvailable
        }
        ConversationAdmissionCloseDisposition::Conflict => {
            runtime_pb::ConversationAdmissionCloseDisposition::Conflict
        }
    };
    runtime_pb::CloseConversationAdmissionResponse {
        disposition: disposition as i32,
        revision: response.revision,
        error: response.error.map(typed_error_to_pb),
    }
}

pub(super) fn reserve_response_to_pb(
    response: ReserveMessageIdentityResponse,
) -> runtime_pb::ReserveMessageIdentityResponse {
    let disposition = match response.disposition {
        MessageIdentityDisposition::Created => runtime_pb::MessageIdentityDisposition::Created,
        MessageIdentityDisposition::ExactReplay => {
            runtime_pb::MessageIdentityDisposition::ExactReplay
        }
        MessageIdentityDisposition::Conflict => runtime_pb::MessageIdentityDisposition::Conflict,
        MessageIdentityDisposition::ConversationNotAvailable => {
            runtime_pb::MessageIdentityDisposition::ConversationNotAvailable
        }
    };
    runtime_pb::ReserveMessageIdentityResponse {
        disposition: disposition as i32,
        identity: response.identity.map(message_identity_to_pb),
        error: response.error.map(typed_error_to_pb),
    }
}
