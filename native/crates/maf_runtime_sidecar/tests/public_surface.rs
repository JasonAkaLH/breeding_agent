use maf_runtime_sidecar::{
    AcknowledgeSubmissionHandoffRequest, AcknowledgeSubmissionProjectionRequest,
    AdmitSubmissionRequest, AdmitSubmissionResponse, ClaimPendingSubmissionRequest,
    ClaimPendingSubmissionResponse, CloseConversationAdmissionRequest,
    CloseConversationAdmissionResponse, ConversationAdmissionCloseDisposition,
    GRPC_MAX_MESSAGE_BYTES, GetSubmissionPreparationRequest, GetSubmissionPreparationResponse,
    MessageIdentityDisposition, MessageIdentityKind, MessageIdentityRecord,
    PrepareSubmissionHandoffRequest, RenewSubmissionClaimRequest, ReserveMessageIdentityRequest,
    ReserveMessageIdentityResponse, SUBMISSION_CONTINUATION_MAX_BYTES,
    SUBMISSION_CONVERSATION_PROJECTION_MAX_BYTES, SUBMISSION_MESSAGE_PROJECTION_MAX_BYTES,
    SUBMISSION_PREPARED_EXECUTION_MAX_BYTES, SubmissionAdmissionDisposition,
    SubmissionAdmissionRecord, SubmissionAdmissionResponse, SubmissionClaim,
    SubmissionHandoffState, SubmissionPreparationState, SubmissionProjectionState,
};

#[test]
fn submission_admission_public_surface_and_wire_limits_are_stable() {
    fn assert_public<T>() {}
    assert_public::<AdmitSubmissionRequest>();
    assert_public::<AdmitSubmissionResponse>();
    assert_public::<ClaimPendingSubmissionRequest>();
    assert_public::<ClaimPendingSubmissionResponse>();
    assert_public::<RenewSubmissionClaimRequest>();
    assert_public::<AcknowledgeSubmissionProjectionRequest>();
    assert_public::<PrepareSubmissionHandoffRequest>();
    assert_public::<GetSubmissionPreparationRequest>();
    assert_public::<GetSubmissionPreparationResponse>();
    assert_public::<AcknowledgeSubmissionHandoffRequest>();
    assert_public::<SubmissionAdmissionResponse>();
    assert_public::<CloseConversationAdmissionRequest>();
    assert_public::<CloseConversationAdmissionResponse>();
    assert_public::<ReserveMessageIdentityRequest>();
    assert_public::<ReserveMessageIdentityResponse>();
    assert_public::<SubmissionAdmissionRecord>();
    assert_public::<SubmissionClaim>();
    assert_public::<MessageIdentityRecord>();
    assert_public::<SubmissionAdmissionDisposition>();
    assert_public::<SubmissionProjectionState>();
    assert_public::<SubmissionPreparationState>();
    assert_public::<SubmissionHandoffState>();
    assert_public::<MessageIdentityKind>();
    assert_public::<MessageIdentityDisposition>();
    assert_public::<ConversationAdmissionCloseDisposition>();
    let request_idempotency_key: fn(&AdmitSubmissionRequest) -> &str =
        |request| &request.idempotency_key;
    let record_idempotency_key: fn(&SubmissionAdmissionRecord) -> &str =
        |record| &record.idempotency_key;
    let _ = (request_idempotency_key, record_idempotency_key);

    assert_eq!(SUBMISSION_CONVERSATION_PROJECTION_MAX_BYTES, 64 * 1024);
    assert_eq!(SUBMISSION_MESSAGE_PROJECTION_MAX_BYTES, 64 * 1024 * 1024);
    assert_eq!(SUBMISSION_CONTINUATION_MAX_BYTES, 64 * 1024 * 1024);
    assert_eq!(SUBMISSION_PREPARED_EXECUTION_MAX_BYTES, 128 * 1024);
    assert!(std::hint::black_box(GRPC_MAX_MESSAGE_BYTES) >= 140 * 1024 * 1024);
}
