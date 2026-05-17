//! Auth primitive kernel for constant-time comparisons and token TTL checks.

use sha2::{Digest, Sha256};
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

    fn secret_missing() -> Self {
        Self {
            code: "auth_secret_missing".to_owned(),
            message: "auth secret is missing".to_owned(),
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

pub fn hmac_sha256_hex(secret: &[u8], payload: &[u8]) -> Result<String, AuthError> {
    if secret.is_empty() {
        return Err(AuthError::secret_missing());
    }
    let mut key_block = [0_u8; 64];
    if secret.len() > key_block.len() {
        let digest = Sha256::digest(secret);
        key_block[..digest.len()].copy_from_slice(&digest);
    } else {
        key_block[..secret.len()].copy_from_slice(secret);
    }

    let mut ipad = [0x36_u8; 64];
    let mut opad = [0x5c_u8; 64];
    for idx in 0..64 {
        ipad[idx] ^= key_block[idx];
        opad[idx] ^= key_block[idx];
    }

    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(payload);
    let inner_digest = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_digest);
    let digest = outer.finalize();
    Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
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

    #[test]
    fn hmac_sha256_is_stable_and_requires_secret() {
        assert_eq!(
            hmac_sha256_hex(b"key", b"The quick brown fox jumps over the lazy dog").unwrap(),
            "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
        );
        assert_eq!(
            hmac_sha256_hex(b"", b"payload").unwrap_err().code,
            "auth_secret_missing"
        );
    }
}
