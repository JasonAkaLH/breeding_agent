fn main() {
    print!(
        "{}",
        maf_skill_runtime::skill_runtime_contract_json().expect("serialize skill runtime contract")
    );
}
