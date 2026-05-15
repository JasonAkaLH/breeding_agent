//! Deterministic task dispatch policy kernel.

use maf_runtime_store::{RuntimeSidecarError, RuntimeSidecarErrorCode};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, VecDeque};

pub const DEFAULT_QUEUE_SIZE: usize = 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskSubmitRequest {
    pub task_id: String,
    pub conversation_id: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskSubmitResult {
    pub task_id: String,
    pub duplicate: bool,
}

#[derive(Debug)]
pub struct TaskDispatcher {
    max_queue: usize,
    queued: VecDeque<String>,
    idempotency: BTreeMap<String, String>,
}

impl Default for TaskDispatcher {
    fn default() -> Self {
        Self::new(DEFAULT_QUEUE_SIZE)
    }
}

impl TaskDispatcher {
    #[must_use]
    pub fn new(max_queue: usize) -> Self {
        Self {
            max_queue,
            queued: VecDeque::new(),
            idempotency: BTreeMap::new(),
        }
    }

    pub fn submit(
        &mut self,
        request: TaskSubmitRequest,
    ) -> Result<TaskSubmitResult, RuntimeSidecarError> {
        if let Some(task_id) = self.idempotency.get(&request.idempotency_key) {
            return Ok(TaskSubmitResult {
                task_id: task_id.clone(),
                duplicate: true,
            });
        }
        if self.queued.len() >= self.max_queue {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::DispatcherQueueFull,
                "dispatcher queue is full",
            ));
        }
        self.idempotency
            .insert(request.idempotency_key, request.task_id.clone());
        self.queued.push_back(request.task_id.clone());
        Ok(TaskSubmitResult {
            task_id: request.task_id,
            duplicate: false,
        })
    }

    #[must_use]
    pub fn pop_next(&mut self) -> Option<String> {
        self.queued.pop_front()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(task_id: &str, key: &str) -> TaskSubmitRequest {
        TaskSubmitRequest {
            task_id: task_id.to_owned(),
            conversation_id: "conv".to_owned(),
            idempotency_key: key.to_owned(),
        }
    }

    #[test]
    fn submit_is_idempotent_by_key() {
        let mut dispatcher = TaskDispatcher::default();
        let first = dispatcher.submit(request("task-1", "idem-1")).unwrap();
        let second = dispatcher.submit(request("task-2", "idem-1")).unwrap();
        assert!(!first.duplicate);
        assert!(second.duplicate);
        assert_eq!(second.task_id, "task-1");
    }

    #[test]
    fn queue_full_fails_closed() {
        let mut dispatcher = TaskDispatcher::new(1);
        dispatcher.submit(request("task-1", "idem-1")).unwrap();
        let err = dispatcher
            .submit(request("task-2", "idem-2"))
            .expect_err("queue must be full");
        assert_eq!(err.code, "dispatcher_queue_full");
    }
}
