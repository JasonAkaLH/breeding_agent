use maf_runtime_sidecar::{RuntimeSidecarSqliteAdapter, SUBMISSION_IMPORT_STDIN_MAX_BYTES};
use std::io::{Read, Write};
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

fn read_bounded_stdin() -> Result<Vec<u8>, ()> {
    let mut input = Vec::new();
    let mut buffer = [0_u8; 64 * 1024];
    let mut stdin = std::io::stdin().lock();
    loop {
        let count = stdin.read(&mut buffer).map_err(|_| ())?;
        if count == 0 {
            return Ok(input);
        }
        if input
            .len()
            .checked_add(count)
            .is_none_or(|size| size > SUBMISSION_IMPORT_STDIN_MAX_BYTES)
        {
            return Err(());
        }
        input.extend_from_slice(&buffer[..count]);
    }
}

fn run() -> Result<Vec<u8>, ()> {
    let mut args = std::env::args_os().skip(1);
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--sqlite")) {
        return Err(());
    }
    let path = args.next().ok_or(())?;
    if args.next().is_some() {
        return Err(());
    }
    let request = read_bounded_stdin()?;
    let finalized_at_ms = i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| ())?
            .as_millis(),
    )
    .map_err(|_| ())?;
    RuntimeSidecarSqliteAdapter::import_submission_authority_file_from_stdin(
        path,
        &request,
        finalized_at_ms,
    )
    .map_err(|_| ())
}

fn main() -> ExitCode {
    match run() {
        Ok(receipt) => {
            if std::io::stdout()
                .write_all(&receipt)
                .and_then(|_| std::io::stdout().write_all(b"\n"))
                .is_ok()
            {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        Err(()) => {
            let _ = std::io::stderr().write_all(b"submission_import_failed\n");
            ExitCode::FAILURE
        }
    }
}
