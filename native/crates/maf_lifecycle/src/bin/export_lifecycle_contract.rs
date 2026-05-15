fn main() {
    print!(
        "{}",
        maf_lifecycle::lifecycle_contract_json().expect("serialize lifecycle contract")
    );
}
