use maf_runtime_sidecar::{
    DEFAULT_LISTEN_ADDR, RuntimeSidecarKernel, RuntimeSidecarServeConfig, serve_runtime_sidecar,
};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("--version") | None => {
            let version = RuntimeSidecarKernel::new().version();
            println!(
                "{} {} {}",
                version.component,
                env!("CARGO_PKG_VERSION"),
                version.protocol_version
            );
            Ok(())
        }
        Some("--serve") => {
            let mut listen_addr = DEFAULT_LISTEN_ADDR.to_owned();
            let mut sqlite_path: Option<String> = None;
            while let Some(arg) = args.next() {
                if arg == "--sqlite" {
                    let Some(value) = args.next() else {
                        return Err("missing value for maf-runtime-sidecar --sqlite".into());
                    };
                    sqlite_path = Some(value);
                } else {
                    listen_addr = arg;
                }
            }
            let mut config = RuntimeSidecarServeConfig::from_listen_addr(&listen_addr)?;
            if let Some(sqlite_path) = sqlite_path {
                config = config.with_sqlite_path(sqlite_path)?;
            }
            serve_runtime_sidecar(config).await?;
            Ok(())
        }
        Some(other) => Err(format!("unknown maf-runtime-sidecar argument: {other}").into()),
    }
}
