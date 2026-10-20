//! Offline, fail-closed consumption of a maintainer-built native bundle.
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::io::{self, Read};
use std::path::{Component, Path, PathBuf};

const RELEASE: &str = "zvec-0.1.154-r1";
const CPU_RELEASE: &str = "zvec-0.1.154-cpu-r1";
pub(super) const REVISION: &str = "5f55650a78f92aff4d48d671423e888fac0469ff";
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

#[derive(Deserialize)]
pub struct Manifest {
    schema: u32,
    release: String,
    crate_version: String,
    llama_cpp_revision: String,
    target: String,
    features: BTreeSet<String>,
    linkage: String,
    link_libraries: Vec<String>,
    files: BTreeMap<String, String>,
}

pub fn sha256(path: &Path) -> io::Result<String> {
    let mut file = fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let length = file.read(&mut buffer)?;
        if length == 0 {
            break;
        }
        hash.update(&buffer[..length]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn payload_path(root: &Path, name: &str) -> Result<PathBuf> {
    let path = Path::new(name);
    if name.contains(['\\', '\n', '\r', ':'])
        || path.as_os_str().is_empty()
        || !path
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
    {
        return Err(format!("invalid payload path: {name:?}").into());
    }
    let mut current = root.to_path_buf();
    for part in path.components() {
        current.push(part);
        if fs::symlink_metadata(&current)?.file_type().is_symlink() {
            return Err(format!("symlinks are not allowed in a native bundle: {name}").into());
        }
    }
    if !current.is_file() {
        return Err(format!("missing payload file: {name}").into());
    }
    Ok(current)
}

fn inventory(root: &Path, directory: &Path, files: &mut BTreeSet<String>) -> Result<()> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        let path = entry.path();
        if kind.is_symlink() {
            return Err("bundle contains an unverified symlink".into());
        }
        if kind.is_dir() {
            inventory(root, &path, files)?;
        } else if kind.is_file() {
            files.insert(
                path.strip_prefix(root)?
                    .to_str()
                    .ok_or("non UTF-8 payload path")?
                    .replace('\\', "/"),
            );
        } else {
            return Err("bundle contains a non-regular payload entry".into());
        }
    }
    Ok(())
}

pub fn validate(root: &Path, target: &str, features: &BTreeSet<String>) -> Result<Manifest> {
    let manifest: Manifest = serde_json::from_slice(&fs::read(root.join("manifest.json"))?)?;
    if manifest.schema != 1
        || ![RELEASE, CPU_RELEASE].contains(&manifest.release.as_str())
        || manifest.crate_version != "0.1.154"
        || manifest.llama_cpp_revision != REVISION
    {
        return Err("bundle version/ABI does not match this crate".into());
    }
    if manifest.target != target {
        return Err(format!("target mismatch: {} != {target}", manifest.target).into());
    }
    if &manifest.features != features {
        return Err(format!(
            "feature mismatch: bundle {:?}, consumer {features:?}",
            manifest.features
        )
        .into());
    }
    let dynamic = features.contains("dynamic-link");
    if manifest.linkage != if dynamic { "dynamic" } else { "static" } {
        return Err("bundle linkage does not match enabled features".into());
    }
    if !manifest.files.contains_key("bindings.rs") || manifest.link_libraries.is_empty() {
        return Err("bundle is missing bindings or link metadata".into());
    }
    let mut actual = BTreeSet::new();
    inventory(root, root, &mut actual)?;
    let mut expected: BTreeSet<_> = manifest.files.keys().cloned().collect();
    expected.insert("manifest.json".into());
    if actual != expected {
        return Err("bundle contains missing or unverified payload files".into());
    }
    for (name, expected) in &manifest.files {
        let path = payload_path(root, name)?;
        if sha256(&path)? != *expected {
            return Err(format!("SHA-256 mismatch: {name}").into());
        }
    }
    for library in &manifest.link_libraries {
        if library.is_empty()
            || !library
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_+-=.".contains(&b))
        {
            return Err(format!("invalid link library: {library:?}").into());
        }
        let name = library.rsplit('=').next().unwrap();
        if name.starts_with("llama") || name.starts_with("ggml") {
            let filename = if target.contains("windows") {
                format!("lib/{name}.lib")
            } else if library.starts_with("static=") {
                format!("lib/lib{name}.a")
            } else {
                format!("lib/lib{name}.so")
            };
            if !manifest.files.contains_key(&filename) {
                return Err(
                    format!("link library is not in the verified payload: {filename}").into(),
                );
            }
        }
    }
    Ok(manifest)
}

pub(super) fn enabled_features() -> BTreeSet<String> {
    // These are the sys crate's native-affecting Cargo features. Keep in sync
    // with Cargo.toml; mismatches are errors, never source-build fallbacks.
    [
        ("common", cfg!(feature = "common")),
        ("cuda", cfg!(feature = "cuda")),
        ("cuda-no-vmm", cfg!(feature = "cuda-no-vmm")),
        ("metal", cfg!(feature = "metal")),
        ("dynamic-link", cfg!(feature = "dynamic-link")),
        ("dynamic-backends", cfg!(feature = "dynamic-backends")),
        ("vulkan", cfg!(feature = "vulkan")),
        ("opencl", cfg!(feature = "opencl")),
        ("mkl", cfg!(feature = "mkl")),
        ("openmp", cfg!(feature = "openmp")),
        ("static-openmp", cfg!(feature = "static-openmp")),
        ("rocm", cfg!(feature = "rocm")),
        ("static-stdcxx", cfg!(feature = "static-stdcxx")),
        ("shared-stdcxx", cfg!(feature = "shared-stdcxx")),
        ("system-ggml", cfg!(feature = "system-ggml")),
        ("system-ggml-static", cfg!(feature = "system-ggml-static")),
        ("mtmd", cfg!(feature = "mtmd")),
    ]
    .into_iter()
    .filter(|(_, on)| *on)
    .map(|(name, _)| name.into())
    .collect()
}

pub fn build() -> Result<()> {
    println!("cargo:rerun-if-env-changed=LLAMA_CPP_RUNTIME_MANIFEST_SHA256");
    let root = PathBuf::from(env::var("LLAMA_CPP_RUNTIME_DIR")?).canonicalize()?;
    let manifest_path = root.join("manifest.json");
    println!("cargo:rerun-if-changed={}", manifest_path.display());
    let expected = env::var("LLAMA_CPP_RUNTIME_MANIFEST_SHA256")
        .map_err(|_| "set LLAMA_CPP_RUNTIME_MANIFEST_SHA256 to the pinned manifest digest")?;
    if sha256(&manifest_path)? != expected {
        return Err("manifest SHA-256 mismatch".into());
    }
    // Source configuration knobs must not silently pretend to alter a prebuilt binary.
    for (name, _) in env::vars() {
        if name.starts_with("GGML_")
            || name.starts_with("CMAKE_") && name != "CMAKE_BUILD_PARALLEL_LEVEL"
            || [
                "LLAMA_LIB_PROFILE",
                "LLAMA_STATIC_CRT",
                "LLAMA_BUILD_SHARED_LIBS",
            ]
            .contains(&name.as_str())
        {
            println!("cargo:rerun-if-env-changed={name}");
            return Err(
                format!("{name} configures source builds, not external bundles; unset it").into(),
            );
        }
    }
    build_verified(&root, &expected)
}

// Auto mode already compared native options. Explicit external mode retains
// its original strict, offline environment validation above.
pub(super) fn build_verified(root: &Path, expected: &str) -> Result<()> {
    let manifest_path = root.join("manifest.json");
    println!("cargo:rerun-if-changed={}", manifest_path.display());
    if sha256(&manifest_path)? != expected {
        return Err("manifest SHA-256 mismatch".into());
    }
    let features = enabled_features();
    let manifest = validate(root, &env::var("TARGET")?, &features)?;
    for file in manifest.files.keys() {
        println!("cargo:rerun-if-changed={}", root.join(file).display());
    }
    fs::copy(
        root.join("bindings.rs"),
        PathBuf::from(env::var("OUT_DIR")?).join("bindings.rs"),
    )?;
    println!(
        "cargo:rustc-link-search=native={}",
        root.join("lib").display()
    );
    // Apple still needs its platform SDK/linker for Rust itself. Locate the
    // compiler runtime exactly as upstream does; do not invoke a C++ build.
    if env::var("TARGET")?.ends_with("-apple-darwin") {
        if let Some(path) = super::macos_link_search_path() {
            println!("cargo:rustc-link-search={path}");
        }
    }
    for library in &manifest.link_libraries {
        println!("cargo:rustc-link-lib={library}");
    }
    if features.contains("dynamic-backends") {
        println!("cargo:backends_dir={}", root.join("lib").display());
    }
    println!("cargo:runtime_dir={}", root.display());
    println!("cargo:runtime_manifest={}", manifest_path.display());
    println!("cargo:warning=Using verified external llama.cpp runtime {}; no bindgen/CMake/C++/CUDA compilation", root.display());
    Ok(())
}
