from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class MailboxMessageType(StrEnum):
    NODE_ASSIGNMENT = "node_assignment"
    NODE_STATUS_REPORT = "node_status_report"
    NODE_RESULT_REPORT = "node_result_report"
    NODE_BLOCKED_REPORT = "node_blocked_report"
    CANCEL_NOTICE = "cancel_notice"
    RESUME_NOTICE = "resume_notice"
    CLARIFICATION_REQUEST = "clarification_request"
    CLARIFICATION_ANSWER = "clarification_answer"
    DEPENDENCY_REQUEST = "dependency_request"
    DEPENDENCY_RESPONSE = "dependency_response"
    ARTIFACT_REFERENCE_SHARE = "artifact_reference_share"
    PEER_CONTEXT_REQUEST = "peer_context_request"
    PEER_CONTEXT_RESPONSE = "peer_context_response"


STRONG_ACK_MESSAGE_TYPES = frozenset(
    {
        MailboxMessageType.NODE_ASSIGNMENT,
        MailboxMessageType.CANCEL_NOTICE,
        MailboxMessageType.RESUME_NOTICE,
        MailboxMessageType.CLARIFICATION_REQUEST,
        MailboxMessageType.CLARIFICATION_ANSWER,
        MailboxMessageType.NODE_BLOCKED_REPORT,
    }
)


LIGHT_ACK_MESSAGE_TYPES = frozenset(
    {
        MailboxMessageType.NODE_STATUS_REPORT,
        MailboxMessageType.NODE_RESULT_REPORT,
        MailboxMessageType.DEPENDENCY_REQUEST,
        MailboxMessageType.DEPENDENCY_RESPONSE,
        MailboxMessageType.ARTIFACT_REFERENCE_SHARE,
        MailboxMessageType.PEER_CONTEXT_REQUEST,
        MailboxMessageType.PEER_CONTEXT_RESPONSE,
    }
)
