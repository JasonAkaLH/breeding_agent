use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let protoc = protoc_bin_vendored::protoc_bin_path()?;
    // Build scripts run before crate compilation; setting PROTOC here is scoped to
    // this process and its children so tonic/prost can generate bindings without
    // requiring a host-level protoc installation.
    unsafe {
        std::env::set_var("PROTOC", protoc);
    }

    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_root = manifest_dir.join("../../proto");
    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(
            &[
                proto_root.join("maf/common/v1/common.proto"),
                proto_root.join("maf/runtime/v1/runtime.proto"),
            ],
            &[proto_root],
        )?;
    Ok(())
}
