fn main() {
    print!(
        "{}",
        maf_core_types::core_contract_json().expect("serialize core contract")
    );
}
