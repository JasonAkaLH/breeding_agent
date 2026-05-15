fn main() {
    print!(
        "{}",
        maf_mcp_runtime::mcp_runtime_contract_json().expect("serialize mcp runtime contract")
    );
}
