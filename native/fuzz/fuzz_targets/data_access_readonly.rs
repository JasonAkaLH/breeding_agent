#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let sql = String::from_utf8_lossy(data);
    let _ = maf_data_access::ensure_readonly_sql(&sql);
    let row_count = data.first().copied().unwrap_or_default() as usize;
    let column_count = data.get(1).copied().unwrap_or_default() as usize;
    let result_bytes = data.len();
    let _ = maf_data_access::validate_shape(row_count, column_count, result_bytes);
});
