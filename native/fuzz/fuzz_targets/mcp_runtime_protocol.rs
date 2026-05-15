#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let _ = maf_mcp_runtime::validate_json_rpc_request(data);
    let raw = String::from_utf8_lossy(data);
    let _ = maf_mcp_runtime::sanitize_tool_output(&raw);
});
