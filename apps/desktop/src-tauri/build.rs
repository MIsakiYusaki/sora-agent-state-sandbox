use std::{
    env, fs,
    path::{Path, PathBuf},
};

fn main() {
    tauri_build::build();

    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("manifest dir"));
    configure_gateway_integrity(&manifest_dir);
    let schema_path = manifest_dir.join("../../../contracts/desktop/desktop-contracts.schema.json");
    println!("cargo:rerun-if-changed={}", schema_path.display());

    let raw = fs::read_to_string(&schema_path).expect("read Desktop contract schema");
    let schema: schemars::schema::RootSchema =
        serde_json::from_str(&raw).expect("parse Desktop contract schema");
    let mut type_space = typify::TypeSpace::default();
    type_space
        .add_root_schema(schema)
        .expect("generate Rust Desktop contract types");
    let output = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR")).join("desktop_contracts.rs");
    fs::write(output, type_space.to_stream().to_string())
        .expect("write generated Rust Desktop contracts");
}

fn configure_gateway_integrity(manifest_dir: &Path) {
    let sidecar_manifest = manifest_dir.join("binaries/sora-gateway-manifest.json");
    println!("cargo:rerun-if-changed={}", sidecar_manifest.display());
    let target = env::var("TARGET").expect("Rust target triple");
    let payload = fs::read_to_string(&sidecar_manifest)
        .ok()
        .and_then(|raw| {
            serde_json::from_str::<serde_json::Value>(raw.trim_start_matches('\u{feff}')).ok()
        })
        .filter(|value| {
            value
                .get("schema_version")
                .and_then(serde_json::Value::as_str)
                == Some("SoraGatewaySidecarManifest.v1")
                && value
                    .get("protocol_version")
                    .and_then(serde_json::Value::as_str)
                    == Some("1.0")
                && value.get("target").and_then(serde_json::Value::as_str) == Some(target.as_str())
        });
    let sha256 = payload
        .as_ref()
        .and_then(|value| value.get("sha256"))
        .and_then(serde_json::Value::as_str)
        .filter(|value| value.len() == 64)
        .unwrap_or("");
    let size = payload
        .as_ref()
        .and_then(|value| value.get("size_bytes"))
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let version = payload
        .as_ref()
        .and_then(|value| value.get("version"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    println!("cargo:rustc-env=SORA_GATEWAY_SHA256={sha256}");
    println!("cargo:rustc-env=SORA_GATEWAY_SIZE={size}");
    println!("cargo:rustc-env=SORA_GATEWAY_VERSION={version}");
}
