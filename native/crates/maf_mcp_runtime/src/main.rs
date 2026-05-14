fn main() {
    let version = maf_mcp_runtime::VersionInfo::current();
    println!(
        "{} {} {}",
        version.component, version.build_version, version.protocol_version
    );
}
