//! Deterministic event append/replay kernel for the runtime sidecar.

use maf_runtime_store::{RuntimeSidecarError, RuntimeSidecarErrorCode};
use serde::{Deserialize, Serialize};

pub const MAX_EVENT_PAYLOAD_BYTES: usize = 256 * 1024;
pub const MAX_REPLAY_EVENTS: usize = 1_000;
pub const MAX_REPLAY_BYTES: usize = 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventRecord {
    pub conversation_id: String,
    pub task_id: String,
    pub sequence: u64,
    pub event_type: String,
    pub payload_json: Vec<u8>,
    pub created_at_ms: i64,
}

#[derive(Debug, Default)]
pub struct EventLog {
    events: Vec<EventRecord>,
}

impl EventLog {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn append(
        &mut self,
        conversation_id: impl Into<String>,
        task_id: impl Into<String>,
        event_type: impl Into<String>,
        payload_json: Vec<u8>,
        created_at_ms: i64,
    ) -> Result<EventRecord, RuntimeSidecarError> {
        if payload_json.len() > MAX_EVENT_PAYLOAD_BYTES {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::EventLogPayloadTooLarge,
                "event payload exceeds configured limit",
            ));
        }
        let conversation_id = conversation_id.into();
        let task_id = task_id.into();
        let next_sequence = self
            .events
            .iter()
            .filter(|event| event.conversation_id == conversation_id && event.task_id == task_id)
            .map(|event| event.sequence)
            .max()
            .unwrap_or(0)
            + 1;
        let event = EventRecord {
            conversation_id,
            task_id,
            sequence: next_sequence,
            event_type: event_type.into(),
            payload_json,
            created_at_ms,
        };
        self.events.push(event.clone());
        Ok(event)
    }

    pub fn replay(
        &self,
        conversation_id: &str,
        task_id: &str,
        after_sequence: u64,
        max_events: usize,
        max_bytes: usize,
    ) -> Result<Vec<EventRecord>, RuntimeSidecarError> {
        let max_events = max_events.min(MAX_REPLAY_EVENTS);
        let max_bytes = max_bytes.min(MAX_REPLAY_BYTES);
        let mut used_bytes = 0usize;
        let mut result = Vec::new();
        for event in self.events.iter().filter(|event| {
            event.conversation_id == conversation_id
                && event.task_id == task_id
                && event.sequence > after_sequence
        }) {
            if result.len() >= max_events {
                break;
            }
            used_bytes += event.payload_json.len();
            if used_bytes > max_bytes {
                return Err(RuntimeSidecarError::new(
                    RuntimeSidecarErrorCode::EventLogReplayPageExceeded,
                    "event replay page exceeds byte limit",
                ));
            }
            result.push(event.clone());
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn append_assigns_monotonic_task_scoped_sequences() {
        let mut log = EventLog::new();
        let first = log
            .append("conv", "task", "task.accepted", b"{}".to_vec(), 1)
            .unwrap();
        let second = log
            .append("conv", "task", "task.running", b"{}".to_vec(), 2)
            .unwrap();
        assert_eq!(first.sequence, 1);
        assert_eq!(second.sequence, 2);
    }

    #[test]
    fn oversized_payload_fails_closed() {
        let mut log = EventLog::new();
        let err = log
            .append(
                "conv",
                "task",
                "large",
                vec![b'x'; MAX_EVENT_PAYLOAD_BYTES + 1],
                1,
            )
            .expect_err("oversized payload must fail");
        assert_eq!(err.code, "event_log_payload_too_large");
    }

    #[test]
    fn replay_uses_single_cursor_semantics() {
        let mut log = EventLog::new();
        log.append("conv", "task", "one", b"1".to_vec(), 1).unwrap();
        log.append("conv", "task", "two", b"2".to_vec(), 2).unwrap();
        let replayed = log.replay("conv", "task", 1, 1000, 1024).unwrap();
        assert_eq!(replayed.len(), 1);
        assert_eq!(replayed[0].event_type, "two");
    }
}
