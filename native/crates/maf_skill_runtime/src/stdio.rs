use std::io::{Read, Write};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::{
    ExecuteSandboxedResponse, SkillRuntimeError, SkillRuntimeErrorCode, TypedErrorEnvelope,
};

const STDIO_DRAIN_GRACE_MS: u64 = 5_000;

pub(super) fn stdio_drain_deadline() -> Instant {
    Instant::now() + Duration::from_millis(STDIO_DRAIN_GRACE_MS)
}

pub(super) fn bounded_output_response(
    exit_code: i32,
    stdout_prefix: Vec<u8>,
    stdout_truncated: bool,
    stderr_prefix: Vec<u8>,
    stderr_truncated: bool,
) -> ExecuteSandboxedResponse {
    let error = if stdout_truncated || stderr_truncated {
        Some(TypedErrorEnvelope::from(SkillRuntimeError::new(
            SkillRuntimeErrorCode::OutputTooLarge,
            "sandbox stdout or stderr exceeded configured limit",
        )))
    } else {
        None
    };
    ExecuteSandboxedResponse {
        exit_code,
        stdout_prefix,
        stderr_prefix,
        stdout_truncated,
        stderr_truncated,
        error,
    }
}

pub(super) fn spawn_stdin_writer(
    mut stdin: std::process::ChildStdin,
    payload: Vec<u8>,
) -> mpsc::Receiver<Result<(), SkillRuntimeError>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let result = stdin.write_all(&payload).map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox stdin write failed",
            )
        });
        let _ = sender.send(result);
    });
    receiver
}

#[derive(Debug)]
pub(super) struct LimitedReaderHandle {
    pub(super) state: Arc<Mutex<LimitedReaderState>>,
    pub(super) done: mpsc::Receiver<()>,
}

#[derive(Debug, Clone)]
pub(super) struct LimitedReaderState {
    pub(super) prefix: Vec<u8>,
    pub(super) truncated: bool,
    pub(super) error: Option<SkillRuntimeError>,
}

pub(super) fn spawn_limited_reader<R>(
    mut pipe: R,
    limit: usize,
    stream_name: &'static str,
) -> LimitedReaderHandle
where
    R: Read + Send + 'static,
{
    let (sender, receiver) = mpsc::channel();
    let state = Arc::new(Mutex::new(LimitedReaderState {
        prefix: Vec::with_capacity(limit.min(8192)),
        truncated: false,
        error: None,
    }));
    let state_for_thread = Arc::clone(&state);
    thread::spawn(move || {
        let mut buffer = [0_u8; 8192];
        if let Err(error) = read_limited_prefix(
            &mut pipe,
            &state_for_thread,
            &mut buffer,
            limit,
            stream_name,
        ) && let Ok(mut state) = state_for_thread.lock()
        {
            state.error = Some(error);
        }
        let _ = sender.send(());
    });
    LimitedReaderHandle {
        state,
        done: receiver,
    }
}

pub(super) fn receive_stdin_writer(
    receiver: Option<mpsc::Receiver<Result<(), SkillRuntimeError>>>,
    deadline: Instant,
) -> Result<(), SkillRuntimeError> {
    match receiver {
        Some(receiver) => receive_before_deadline(receiver, deadline),
        None => Ok(()),
    }
}

pub(super) fn receive_limited_reader(
    receiver: Option<LimitedReaderHandle>,
    deadline: Instant,
) -> Result<(Vec<u8>, bool), SkillRuntimeError> {
    match receiver {
        Some(receiver) => receive_reader_before_deadline(receiver, deadline),
        None => Ok((Vec::new(), false)),
    }
}

pub(super) fn receive_reader_before_deadline(
    receiver: LimitedReaderHandle,
    deadline: Instant,
) -> Result<(Vec<u8>, bool), SkillRuntimeError> {
    let now = Instant::now();
    let timeout = deadline.saturating_duration_since(now);
    let _ = receiver.done.recv_timeout(timeout);
    snapshot_limited_reader(&receiver)
}

pub(super) fn snapshot_limited_reader(
    receiver: &LimitedReaderHandle,
) -> Result<(Vec<u8>, bool), SkillRuntimeError> {
    let state = receiver.state.lock().map_err(|_| {
        SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxPolicyDenied,
            "sandbox stdio reader state is poisoned",
        )
    })?;
    if let Some(error) = state.error.clone() {
        return Err(error);
    }
    Ok((state.prefix.clone(), state.truncated))
}

pub(super) fn receive_before_deadline<T>(
    receiver: mpsc::Receiver<Result<T, SkillRuntimeError>>,
    deadline: Instant,
) -> Result<T, SkillRuntimeError> {
    let now = Instant::now();
    let timeout = deadline.saturating_duration_since(now);
    receiver.recv_timeout(timeout).map_err(|_| {
        SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxTimeout,
            "sandbox stdio did not close before deadline",
        )
    })?
}

pub(super) fn read_limited_prefix<R>(
    pipe: &mut R,
    state: &Arc<Mutex<LimitedReaderState>>,
    buffer: &mut [u8; 8192],
    limit: usize,
    stream_name: &str,
) -> Result<(), SkillRuntimeError>
where
    R: Read,
{
    loop {
        let read = pipe.read(buffer).map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                format!("sandbox {stream_name} collection failed"),
            )
        })?;
        if read == 0 {
            return Ok(());
        }

        let mut state = state.lock().map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox stdio reader state is poisoned",
            )
        })?;
        let remaining = limit.saturating_sub(state.prefix.len());
        if remaining > 0 {
            let to_copy = remaining.min(read);
            state.prefix.extend_from_slice(&buffer[..to_copy]);
        }
        if read > remaining {
            state.truncated = true;
        }
    }
}
