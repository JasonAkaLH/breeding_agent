fn main() {
    print!(
        "{}",
        maf_runtime_store::runtime_sidecar_contract_json()
            .expect("serialize runtime sidecar contract")
    );
}
