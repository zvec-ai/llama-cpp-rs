use super::external_contract::{sha256, validate};
use serde_json::{json, Value};
use std::{collections::BTreeSet, fs, path::Path};

fn fixture() -> (tempfile::TempDir, Value, BTreeSet<String>) {
    let temp = tempfile::tempdir().unwrap();
    fs::write(temp.path().join("bindings.rs"), "// generated fixture\n").unwrap();
    let manifest = json!({"schema": 1, "release": "zvec-0.1.154-r1", "crate_version": "0.1.154",
        "llama_cpp_revision": "5f55650a78f92aff4d48d671423e888fac0469ff",
        "target": "aarch64-apple-darwin", "features": ["common", "metal"],
        "linkage": "static", "link_libraries": ["c++"],
        "files": {"bindings.rs": sha256(&temp.path().join("bindings.rs")).unwrap()}});
    (temp, manifest, ["common".into(), "metal".into()].into())
}

fn save(root: &Path, manifest: &Value) {
    fs::write(
        root.join("manifest.json"),
        serde_json::to_vec(manifest).unwrap(),
    )
    .unwrap();
}

#[test]
fn valid_bundle_and_corruption() {
    let (temp, manifest, features) = fixture();
    save(temp.path(), &manifest);
    assert!(validate(temp.path(), "aarch64-apple-darwin", &features).is_ok());
    fs::write(temp.path().join("bindings.rs"), "corrupt").unwrap();
    assert!(validate(temp.path(), "aarch64-apple-darwin", &features)
        .err()
        .unwrap()
        .to_string()
        .contains("SHA-256 mismatch"));
}

#[test]
fn reject_abi_target_features_and_linkage_mismatch() {
    let (temp, manifest, features) = fixture();
    for (key, value) in [
        ("schema", json!(2)),
        ("release", json!("wrong")),
        ("llama_cpp_revision", json!("wrong")),
        ("crate_version", json!("0.0.0")),
        ("target", json!("x86_64-pc-windows-msvc")),
        ("features", json!(["openmp"])),
        ("linkage", json!("dynamic")),
        ("link_libraries", json!(["bad\ncargo:rustc-cfg=injected"])),
    ] {
        let mut changed = manifest.clone();
        changed[key] = value;
        save(temp.path(), &changed);
        assert!(
            validate(temp.path(), "aarch64-apple-darwin", &features).is_err(),
            "accepted {key}"
        );
    }
}

#[test]
fn reject_missing_files_and_unsafe_paths() {
    let (temp, manifest, features) = fixture();
    for path in [
        "../outside",
        "/absolute",
        "C:\\outside",
        "lib/../../outside",
        "missing",
    ] {
        let mut changed = manifest.clone();
        changed["files"][path] = json!("0".repeat(64));
        save(temp.path(), &changed);
        assert!(
            validate(temp.path(), "aarch64-apple-darwin", &features).is_err(),
            "accepted {path}"
        );
    }
}

#[cfg(unix)]
#[test]
fn reject_payload_symlinks() {
    let (temp, mut manifest, features) = fixture();
    std::os::unix::fs::symlink("bindings.rs", temp.path().join("alias")).unwrap();
    manifest["files"]["alias"] = manifest["files"]["bindings.rs"].clone();
    save(temp.path(), &manifest);
    assert!(validate(temp.path(), "aarch64-apple-darwin", &features).is_err());
}
