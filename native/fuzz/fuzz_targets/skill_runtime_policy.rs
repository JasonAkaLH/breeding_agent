#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let payload = String::from_utf8_lossy(data);
    let _ = maf_skill_runtime::skill_policy_validate_json(&payload);
});
