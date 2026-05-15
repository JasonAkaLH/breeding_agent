//! Auth primitive kernel for constant-time comparisons and token TTL checks.

use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct AuthError {
    pub code: String,
    pub message: String,
}

impl AuthError {
    fn invalid() -> Self {
        Self {
            code: "auth_token_invalid".to_owned(),
            message: "auth token validation failed".to_owned(),
        }
    }
}

#[must_use]
pub fn constant_time_eq(expected: &[u8], actual: &[u8]) -> bool {
    let max_len = expected.len().max(actual.len());
    let mut diff = expected.len() ^ actual.len();
    for idx in 0..max_len {
        let left = expected.get(idx).copied().unwrap_or(0);
        let right = actual.get(idx).copied().unwrap_or(0);
        diff |= usize::from(left ^ right);
    }
    diff == 0
}

pub fn verify_token(expected: &[u8], actual: &[u8]) -> Result<(), AuthError> {
    if constant_time_eq(expected, actual) {
        Ok(())
    } else {
        Err(AuthError::invalid())
    }
}

#[must_use]
pub fn expires_at_ms(issued_at_ms: i64, ttl_ms: i64) -> i64 {
    issued_at_ms.saturating_add(ttl_ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_verify_uses_stable_error() {
        assert!(verify_token(b"same", b"same").is_ok());
        assert_eq!(
            verify_token(b"same", b"diff").unwrap_err().code,
            "auth_token_invalid"
        );
    }

    #[test]
    fn ttl_saturates() {
        assert_eq!(expires_at_ms(100, 50), 150);
        assert_eq!(expires_at_ms(i64::MAX, 50), i64::MAX);
    }
}
