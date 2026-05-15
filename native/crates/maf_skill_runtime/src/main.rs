use maf_skill_runtime::{
    DEFAULT_SKILL_SANDBOX_LISTEN_ADDR, SkillSandboxServeConfig, SkillSandboxService,
    serve_skill_sandbox,
};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("--version") | None => {
            let version = SkillSandboxService::new().version();
            println!(
                "{} {} {}",
                version.component,
                env!("CARGO_PKG_VERSION"),
                version.protocol_version
            );
            Ok(())
        }
        Some("--serve") => {
            let mut listen_addr = DEFAULT_SKILL_SANDBOX_LISTEN_ADDR.to_owned();
            let mut sandbox_root: Option<String> = None;
            while let Some(arg) = args.next() {
                if arg == "--sandbox-root" {
                    let Some(value) = args.next() else {
                        return Err("missing value for maf-skill-sandbox --sandbox-root".into());
                    };
                    sandbox_root = Some(value);
                } else {
                    listen_addr = arg;
                }
            }
            let mut config = SkillSandboxServeConfig::from_listen_addr(&listen_addr)?;
            if let Some(sandbox_root) = sandbox_root {
                config = config.with_sandbox_root(sandbox_root)?;
            }
            serve_skill_sandbox(config).await?;
            Ok(())
        }
        Some(other) => Err(format!("unknown maf-skill-sandbox argument: {other}").into()),
    }
}
