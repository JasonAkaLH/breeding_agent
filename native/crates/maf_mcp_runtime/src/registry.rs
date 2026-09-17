use super::{
    BundleRegistry, McpRuntimeError, McpRuntimeErrorCode, McpTaskRecord, McpTaskRegistry,
    McpTaskState, TypedError,
};

impl BundleRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn begin_activation(&mut self, revision: impl Into<String>) {
        self.pending_revision = Some(revision.into());
    }

    pub fn commit_pending(
        &mut self,
        validation_passed: bool,
    ) -> Result<Option<String>, McpRuntimeError> {
        if !validation_passed {
            self.pending_revision = None;
            return Err(McpRuntimeError {
                typed_error: TypedError::new(
                    McpRuntimeErrorCode::BundleActivationFailed,
                    "bundle validation failed",
                ),
            });
        }
        self.active_revision = self.pending_revision.take();
        Ok(self.active_revision.clone())
    }
}

impl McpTaskRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn create_task(&mut self, task_id: impl Into<String>) -> McpTaskRecord {
        let task_id = task_id.into();
        let record = McpTaskRecord {
            task_id: task_id.clone(),
            state: McpTaskState::Pending,
        };
        self.tasks.insert(task_id, record.clone());
        record
    }

    pub fn cancel_task(&mut self, task_id: &str) -> Result<McpTaskRecord, McpRuntimeError> {
        let record = self.tasks.get_mut(task_id).ok_or_else(|| McpRuntimeError {
            typed_error: TypedError::new(
                McpRuntimeErrorCode::Cancelled,
                "task is unknown or already gone",
            ),
        })?;
        if matches!(
            record.state,
            McpTaskState::Completed | McpTaskState::Failed | McpTaskState::Cancelled
        ) {
            return Err(McpRuntimeError {
                typed_error: TypedError::new(
                    McpRuntimeErrorCode::Cancelled,
                    "terminal task cannot be cancelled",
                ),
            });
        }
        record.state = McpTaskState::Cancelled;
        Ok(record.clone())
    }
}
