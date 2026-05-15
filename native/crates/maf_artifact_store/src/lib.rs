//! Artifact/upload path and hash safety kernel.

use sha2::{Digest, Sha256};
use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct ArtifactError {
    pub code: String,
    pub message: String,
}

impl ArtifactError {
    fn path_escape() -> Self {
        Self {
            code: "artifact_path_escape".to_owned(),
            message: "artifact path escapes managed root".to_owned(),
        }
    }
}

pub fn normalize_storage_key(key: &str) -> Result<String, ArtifactError> {
    if key.is_empty() || key.starts_with('/') || key.starts_with('~') || key.contains('\\') {
        return Err(ArtifactError::path_escape());
    }
    let mut parts = Vec::new();
    for part in key.split('/') {
        if part.is_empty() || part == "." || part == ".." || part.contains('\0') {
            return Err(ArtifactError::path_escape());
        }
        parts.push(part);
    }
    Ok(parts.join("/"))
}

#[must_use]
pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn storage_key_rejects_path_escape() {
        assert_eq!(
            normalize_storage_key("task/artifact.json").unwrap(),
            "task/artifact.json"
        );
        assert_eq!(
            normalize_storage_key("../secret").unwrap_err().code,
            "artifact_path_escape"
        );
        assert_eq!(
            normalize_storage_key("/tmp/secret").unwrap_err().code,
            "artifact_path_escape"
        );
    }

    #[test]
    fn sha256_is_stable() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        );
    }
}
