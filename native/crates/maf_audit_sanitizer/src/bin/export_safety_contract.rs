fn main() {
    print!(
        "{}",
        maf_audit_sanitizer::safety_contract_json().expect("serialize safety contract")
    );
}
