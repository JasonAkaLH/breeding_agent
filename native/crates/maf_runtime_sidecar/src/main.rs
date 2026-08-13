#[cfg(unix)]
use maf_runtime_sidecar::semantic_probe_runtime_sidecar_unix_socket;
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
            let mut tls_cert_path: Option<String> = None;
            let mut tls_key_path: Option<String> = None;
            let mut tls_client_ca_path: Option<String> = None;
            while let Some(arg) = args.next() {
                if arg == "--sqlite" {
                    let Some(value) = args.next() else {
                        return Err("missing value for maf-runtime-sidecar --sqlite".into());
                    };
                    sqlite_path = Some(value);
                } else if arg == "--tls-cert" {
                    let Some(value) = args.next() else {
                        return Err("missing value for maf-runtime-sidecar --tls-cert".into());
                    };
                    tls_cert_path = Some(value);
                } else if arg == "--tls-key" {
                    let Some(value) = args.next() else {
                        return Err("missing value for maf-runtime-sidecar --tls-key".into());
                    };
                    tls_key_path = Some(value);
                } else if arg == "--client-ca" {
                    let Some(value) = args.next() else {
                        return Err("missing value for maf-runtime-sidecar --client-ca".into());
                    };
                    tls_client_ca_path = Some(value);
                } else {
                    listen_addr = arg;
                }
            }
            let mut config = match (
                tls_cert_path.as_deref(),
                tls_key_path.as_deref(),
                tls_client_ca_path.as_deref(),
            ) {
                (Some(cert), Some(key), Some(client_ca)) => {
                    RuntimeSidecarServeConfig::from_listen_addr_with_mtls_paths(
                        &listen_addr,
                        cert,
                        key,
                        client_ca,
                    )?
                }
                (None, None, None) => RuntimeSidecarServeConfig::from_listen_addr(&listen_addr)?,
                _ => {
                    return Err(
                        "maf-runtime-sidecar mTLS requires --tls-cert, --tls-key, and --client-ca"
                            .into(),
                    );
                }
            };
            if let Some(sqlite_path) = sqlite_path {
                config = config.with_sqlite_path(sqlite_path)?;
            }
            serve_runtime_sidecar(config).await?;
            Ok(())
        }
        #[cfg(unix)]
        Some("--probe") => {
            let endpoint = args
                .next()
                .ok_or("missing value for maf-runtime-sidecar --probe")?;
            if args.next().is_some() {
                return Err("maf-runtime-sidecar --probe accepts exactly one endpoint".into());
            }
            let socket_path = endpoint
                .strip_prefix("unix://")
                .ok_or("maf-runtime-sidecar --probe requires a unix:// endpoint")?;
            semantic_probe_runtime_sidecar_unix_socket(socket_path).await
        }
        Some(other) => Err(format!("unknown maf-runtime-sidecar argument: {other}").into()),
    }
}
