#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let midpoint = data.len() / 2;
    let (left, right) = data.split_at(midpoint);
    let _ = maf_auth_core::constant_time_eq(left, right);
    let _ = maf_auth_core::verify_token(left, right);
    let _ = maf_auth_core::hmac_sha256_hex(left, right);
    let issued_at = i64::from_le_bytes(padded_8(left));
    let ttl = i64::from_le_bytes(padded_8(right));
    let _ = maf_auth_core::expires_at_ms(issued_at, ttl);
});

fn padded_8(bytes: &[u8]) -> [u8; 8] {
    let mut output = [0_u8; 8];
    let len = bytes.len().min(output.len());
    output[..len].copy_from_slice(&bytes[..len]);
    output
}
