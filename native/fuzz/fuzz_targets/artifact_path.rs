#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let key = String::from_utf8_lossy(data);
    let _ = maf_artifact_store::normalize_storage_key(&key);
    let _ = maf_artifact_store::sha256_hex(data);
});
