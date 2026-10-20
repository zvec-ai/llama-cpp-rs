//! Native SDK selection belongs in the sys crate, using Cargo's resolved
//! features and environment. Consumer projects invoke ordinary Cargo commands.
use super::external;
use flate2::read::GzDecoder;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs::{self, OpenOptions};
use std::io;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

#[derive(Deserialize)]
pub struct RuntimeLock {
    pub release: String,
    pub targets: BTreeMap<String, Bundle>,
}

#[derive(Deserialize)]
pub struct Bundle {
    pub archive: String,
    pub sha256: String,
    pub manifest_sha256: String,
    pub features: BTreeSet<String>,
    pub cmake: BTreeMap<String, String>,
}

pub fn runtime_lock() -> Result<RuntimeLock> {
    Ok(serde_json::from_str(include_str!("runtime-lock.json"))?)
}

pub fn cpu_runtime_lock() -> Result<RuntimeLock> {
    Ok(serde_json::from_str(include_str!("cpu-runtime-lock.json"))?)
}

pub fn select_runtime<'a>(
    locks: &'a [RuntimeLock],
    target: &str,
    features: &BTreeSet<String>,
    variables: &BTreeMap<String, String>,
) -> Option<&'a RuntimeLock> {
    locks.iter().find(|lock| {
        lock.targets
            .get(target)
            .is_some_and(|bundle| mismatches(bundle, features, variables).is_empty())
    })
}

const SOURCE_OPTIONS: &[&str] = &[
    "LLAMA_LIB_PROFILE",
    "LLAMA_STATIC_CRT",
    "LLAMA_BUILD_SHARED_LIBS",
];
const COMPILER_OPTIONS: &[&str] = &[
    "CC",
    "CXX",
    "AR",
    "NVCC",
    "CFLAGS",
    "CPPFLAGS",
    "CXXFLAGS",
    "NVCCFLAGS",
];
const OTHER_OPTIONS: &[&str] = &[
    "CMAKE_C_COMPILER",
    "CMAKE_CXX_COMPILER",
    "CMAKE_CUDA_COMPILER",
    "CMAKE_C_FLAGS",
    "CMAKE_C_FLAGS_RELEASE",
    "CMAKE_C_FLAGS_DEBUG",
    "CMAKE_C_FLAGS_RELWITHDEBINFO",
    "CMAKE_C_FLAGS_MINSIZEREL",
    "CMAKE_CXX_FLAGS_DEBUG",
    "CMAKE_CXX_FLAGS_RELWITHDEBINFO",
    "CMAKE_CXX_FLAGS_MINSIZEREL",
    "CMAKE_CUDA_FLAGS",
    "CMAKE_CUDA_FLAGS_RELEASE",
    "CMAKE_CUDA_FLAGS_DEBUG",
    "CMAKE_CUDA_FLAGS_RELWITHDEBINFO",
    "CMAKE_CUDA_FLAGS_MINSIZEREL",
    "CMAKE_TOOLCHAIN_FILE",
    "CMAKE_GENERATOR",
    "CMAKE_MAKE_PROGRAM",
    "CMAKE_VERBOSE",
    "CMAKE_SYSTEM_NAME",
    "CMAKE_SYSTEM_PROCESSOR",
    "CMAKE_OSX_ARCHITECTURES",
    "CMAKE_OSX_SYSROOT",
    "CMAKE_SYSROOT",
    "CMAKE_LINKER",
    "CMAKE_AR",
    "CMAKE_RANLIB",
    "CMAKE_C_STANDARD",
    "CMAKE_CXX_STANDARD",
    "CMAKE_CUDA_STANDARD",
    "CMAKE_INTERPROCEDURAL_OPTIMIZATION",
    "CMAKE_PREFIX_PATH",
    "CMAKE_INSTALL_PREFIX",
    "CMAKE_FIND_ROOT_PATH",
    "CMAKE_MSVC_DEBUG_INFORMATION_FORMAT",
    "CMAKE",
    "CUDACXX",
    "CUDAHOSTCXX",
    "MACOSX_DEPLOYMENT_TARGET",
    "IPHONEOS_DEPLOYMENT_TARGET",
    "CARGO_ENCODED_RUSTFLAGS",
    "LLAMA_CPP_RUNTIME_CACHE",
    "LLAMA_CPP_RUNTIME_ARCHIVE",
    "LLAMA_CPP_OFFLINE",
    "LLAMA_CPP_SOURCE_CACHE",
    "LLAMA_CPP_SOURCE_DIR",
    "LLAMA_CPP_SOURCE_ARCHIVE",
    "CARGO_NET_OFFLINE",
    "CARGO_HOME",
    "HOME",
    "USERPROFILE",
];

pub fn watch_environment() -> Result<()> {
    let locks = [runtime_lock()?, cpu_runtime_lock()?];
    let mut names = BTreeSet::<String>::new();
    // Watch supported options even when unset, so adding an override on the
    // next Cargo invocation invalidates an already-cached prebuilt selection.
    for entry in locks.iter().flat_map(|lock| lock.targets.values()) {
        names.extend(entry.cmake.keys().cloned());
    }
    names.extend(
        SOURCE_OPTIONS
            .iter()
            .chain(OTHER_OPTIONS)
            .map(|s| (*s).into()),
    );
    for option in COMPILER_OPTIONS {
        names.insert((*option).into());
        names.insert(format!("HOST_{option}"));
        names.insert(format!("TARGET_{option}"));
        for target in [
            env::var("TARGET").unwrap_or_default(),
            env::var("HOST").unwrap_or_default(),
        ] {
            names.insert(format!("{option}_{target}"));
            names.insert(format!("{option}_{}", target.replace('-', "_")));
        }
    }
    for name in env::vars().map(|(name, _)| name) {
        if name.starts_with("GGML_") || name.starts_with("CMAKE_") {
            names.insert(name);
        }
    }
    for name in names {
        println!("cargo:rerun-if-env-changed={name}");
    }
    Ok(())
}

fn boolean(value: &str) -> Option<bool> {
    match value.to_ascii_uppercase().as_str() {
        "ON" | "TRUE" | "YES" | "1" => Some(true),
        "OFF" | "FALSE" | "NO" | "0" => Some(false),
        _ => None,
    }
}

fn compiler_override(name: &str) -> bool {
    COMPILER_OPTIONS.iter().any(|option| {
        name == *option
            || name.starts_with(&format!("{option}_"))
            || name == format!("HOST_{option}")
            || name == format!("TARGET_{option}")
    }) || ["CMAKE", "CUDACXX", "CUDAHOSTCXX"].contains(&name)
}

pub fn mismatches(
    bundle: &Bundle,
    features: &BTreeSet<String>,
    variables: &BTreeMap<String, String>,
) -> Vec<String> {
    let mut reasons = Vec::new();
    if &bundle.features != features {
        reasons.push(format!(
            "native features {features:?} differ from SDK {:?}",
            bundle.features
        ));
    }
    for (name, value) in variables {
        if name == "CMAKE_BUILD_PARALLEL_LEVEL" {
            continue;
        }
        let expected = match name.as_str() {
            "LLAMA_LIB_PROFILE" => Some("Release"),
            "LLAMA_STATIC_CRT" => Some("OFF"),
            "LLAMA_BUILD_SHARED_LIBS" => bundle.cmake.get("BUILD_SHARED_LIBS").map(String::as_str),
            "MACOSX_DEPLOYMENT_TARGET" => bundle.cmake.get(name).map(String::as_str),
            _ if name.starts_with("GGML_") || name.starts_with("CMAKE_") => {
                bundle.cmake.get(name).map(String::as_str)
            }
            _ => None,
        };
        if expected.is_some()
            || SOURCE_OPTIONS.contains(&name.as_str())
            || name.starts_with("GGML_")
            || name.starts_with("CMAKE_")
            || name == "MACOSX_DEPLOYMENT_TARGET"
            || name == "IPHONEOS_DEPLOYMENT_TARGET"
        {
            // Preserve upstream's exact `value == "1"` interpretation.
            let actual = if ["LLAMA_STATIC_CRT", "LLAMA_BUILD_SHARED_LIBS"].contains(&name.as_str())
            {
                if value == "1" {
                    "ON"
                } else {
                    "OFF"
                }
            } else {
                value.as_str()
            };
            let matches = expected.is_some_and(|expected| {
                actual == expected
                    || matches!(expected, "ON" | "OFF") && boolean(actual) == boolean(expected)
            });
            if !matches {
                reasons.push(format!("{name}={value:?} differs from SDK {expected:?}"));
            }
        } else if compiler_override(name) && !value.is_empty() {
            reasons.push(format!("native compiler override {name}"));
        } else if name.contains("RUSTFLAGS")
            && [
                "target-cpu",
                "target-feature",
                "target_cpu",
                "target_feature",
                "crt-static",
                "--cfg\u{1f}feature",
                "--cfg feature",
            ]
            .iter()
            .any(|needle| value.contains(needle))
        {
            reasons.push(format!("{name} changes native CPU/CRT configuration"));
        }
    }
    reasons
}

pub fn extract(archive: &Path, destination: &Path) -> Result<()> {
    let decoder = GzDecoder::new(fs::File::open(archive)?);
    let mut tar = tar::Archive::new(decoder);
    let mut names = BTreeSet::new();
    let mut total = 0u64;
    for entry in tar.entries()? {
        let mut entry = entry?;
        let path = entry.path()?.into_owned();
        let name = path.to_str().ok_or("non UTF-8 SDK archive path")?;
        if !entry.header().entry_type().is_file()
            || name.is_empty()
            || name.contains(['\\', ':'])
            || !path
                .components()
                .all(|part| matches!(part, Component::Normal(_)))
            || !names.insert(path.clone())
        {
            return Err(format!("unsafe SDK archive entry: {name:?}").into());
        }
        total = total
            .checked_add(entry.size())
            .ok_or("SDK archive size overflow")?;
        if total > 8 * 1024 * 1024 * 1024 || names.len() > 10000 {
            return Err("SDK archive exceeds extraction limits".into());
        }
        let output = destination.join(&path);
        fs::create_dir_all(output.parent().ok_or("SDK entry has no parent")?)?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(output)?;
        io::copy(&mut entry, &mut file)?;
    }
    Ok(())
}

pub fn verify_bundle(root: &Path, bundle: &Bundle, target: &str) -> Result<()> {
    if external::sha256(&root.join("manifest.json"))? != bundle.manifest_sha256 {
        return Err("SDK manifest SHA-256 mismatch".into());
    }
    external::validate(root, target, &bundle.features)?;
    if !bundle.cmake.is_empty() {
        let manifest: serde_json::Value =
            serde_json::from_slice(&fs::read(root.join("manifest.json"))?)?;
        let mut cmake: BTreeMap<String, String> =
            serde_json::from_value(manifest["primary_cmake"].clone())?;
        for key in [
            "CMAKE_CXX_COMPILER",
            "CMAKE_CUDA_COMPILER",
            "GGML_BACKEND_DIR",
        ] {
            cmake.remove(key);
        }
        if let Some(minimum) = cmake
            .get("CMAKE_CXX_FLAGS")
            .and_then(|flags| {
                flags
                    .split_once("-mmacosx-version-min=")
                    .and_then(|(_, tail)| tail.split_whitespace().next())
            })
            .map(str::to_owned)
        {
            cmake.insert("MACOSX_DEPLOYMENT_TARGET".into(), minimum);
        }
        if cmake != bundle.cmake {
            return Err("SDK native configuration differs from embedded runtime lock".into());
        }
    }
    Ok(())
}

pub fn acquire(
    lock: &RuntimeLock,
    target: &str,
    cache: &Path,
    archive: Option<&Path>,
    offline: bool,
) -> Result<PathBuf> {
    let bundle = lock.targets.get(target).ok_or("no SDK for target")?;
    let parent = cache.join(&bundle.sha256);
    fs::create_dir_all(&parent)?;
    let guard = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(parent.join(".lock"))?;
    guard.lock()?;
    let root = parent.join("bundle");
    if root.exists() {
        verify_bundle(&root, bundle, target)?;
        return Ok(root);
    }
    if offline && archive.is_none() {
        return Err("SDK is not cached; fetch once online, supply LLAMA_CPP_RUNTIME_ARCHIVE, or select source mode".into());
    }
    let temporary = tempfile::Builder::new()
        .prefix(".fetch-")
        .tempdir_in(&parent)?;
    let downloaded = temporary.path().join(&bundle.archive);
    let archive = if let Some(archive) = archive {
        archive
    } else {
        let url = format!(
            "https://github.com/zvec-ai/llama-cpp-rs/releases/download/{}/{}",
            lock.release, bundle.archive
        );
        println!(
            "cargo:warning=Downloading pinned llama.cpp SDK {} for {target}",
            lock.release
        );
        let status = Command::new(if cfg!(windows) { "curl.exe" } else { "curl" })
            .args([
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--retry",
                "3",
                "--connect-timeout",
                "15",
                "--max-time",
                "1200",
                "--output",
            ])
            .arg(&downloaded)
            .arg(url)
            .status()?;
        if !status.success() {
            return Err("SDK download failed; no implicit source fallback (set LLAMA_CPP_BUILD_MODE=source if intended)".into());
        }
        &downloaded
    };
    if external::sha256(archive)? != bundle.sha256 {
        return Err("SDK archive SHA-256 mismatch".into());
    }
    let extracted = temporary.path().join("bundle");
    fs::create_dir(&extracted)?;
    extract(archive, &extracted)?;
    verify_bundle(&extracted, bundle, target)?;
    fs::rename(extracted, &root)?;
    Ok(root)
}

fn cache_directory() -> Result<PathBuf> {
    if let Some(path) = env::var_os("LLAMA_CPP_RUNTIME_CACHE") {
        return Ok(PathBuf::from(path));
    }
    let cargo_home = env::var_os("CARGO_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            env::var_os("HOME")
                .or_else(|| env::var_os("USERPROFILE"))
                .map(|path| PathBuf::from(path).join(".cargo"))
        })
        .ok_or("cannot locate Cargo home; set LLAMA_CPP_RUNTIME_CACHE")?;
    Ok(cargo_home.join("llama-cpp-runtimes"))
}

pub fn offline() -> bool {
    ["LLAMA_CPP_OFFLINE", "CARGO_NET_OFFLINE"]
        .iter()
        .any(|name| env::var(name).ok().as_deref().and_then(boolean) == Some(true))
}

pub fn try_build(prebuilt_only: bool) -> Result<bool> {
    let locks = [runtime_lock()?, cpu_runtime_lock()?];
    let target = env::var("TARGET")?;
    let variables = env::vars().collect();
    let features = external::enabled_features();
    let Some(lock) = select_runtime(&locks, &target, &features, &variables) else {
        let reasons: Vec<_> = locks
            .iter()
            .flat_map(|lock| {
                lock.targets.get(&target).map_or_else(
                    || vec![format!("no pinned SDK for {target} in {}", lock.release)],
                    |bundle| mismatches(bundle, &features, &variables),
                )
            })
            .collect();
        if prebuilt_only {
            return Err(format!("prebuilt mode is incompatible: {}", reasons.join("; ")).into());
        }
        println!(
            "cargo:warning=Building llama.cpp from source: {}",
            reasons.join("; ")
        );
        return Ok(false);
    };
    let archive = env::var_os("LLAMA_CPP_RUNTIME_ARCHIVE").map(PathBuf::from);
    if let Some(archive) = &archive {
        println!("cargo:rerun-if-changed={}", archive.display());
    }
    let root = acquire(
        lock,
        &target,
        &cache_directory()?,
        archive.as_deref(),
        offline(),
    )?;
    external::build_verified(&root, &lock.targets[&target].manifest_sha256)?;
    Ok(true)
}

pub struct SourceConfiguration {
    path: PathBuf,
    fingerprint: String,
}

impl SourceConfiguration {
    // Commit only after CMake completes successfully. Failed configuration
    // must not mark a partial/old CMake cache as matching the new options.
    pub fn commit(&self) -> Result<()> {
        fs::write(&self.path, &self.fingerprint)?;
        Ok(())
    }
}

pub fn prepare_source_configuration(
    out: &Path,
    features: &BTreeSet<String>,
    variables: &BTreeMap<String, String>,
    source: &Path,
) -> Result<SourceConfiguration> {
    let native: BTreeMap<_, _> = variables
        .iter()
        .filter(|(name, _)| {
            name.as_str() != "CMAKE_BUILD_PARALLEL_LEVEL"
                && (name.starts_with("CMAKE_")
                    || name.starts_with("GGML_")
                    || SOURCE_OPTIONS.contains(&name.as_str())
                    || compiler_override(name)
                    || [
                        "TARGET",
                        "HOST",
                        "OPT_LEVEL",
                        "DEBUG",
                        "CARGO_CFG_TARGET_FEATURE",
                        "CARGO_ENCODED_RUSTFLAGS",
                        "MACOSX_DEPLOYMENT_TARGET",
                        "IPHONEOS_DEPLOYMENT_TARGET",
                    ]
                    .contains(&name.as_str()))
        })
        .collect();
    let fingerprint = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&(
            features,
            native,
            source,
            include_str!("source-lock.json")
        ))?)
    );
    fs::create_dir_all(out)?;
    let path = out.join("llama-source-config.sha256");
    let cache = out.join("build/CMakeCache.txt");
    if cache.exists()
        && (fs::read_to_string(&path).ok().as_deref() != Some(&fingerprint)
            || !out.join("build/cmake_install.cmake").is_file())
    {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        fs::rename(
            &cache,
            cache.with_extension(format!("txt.previous-{}-{nanos}", std::process::id())),
        )?;
        println!("cargo:warning=Native options changed or prior CMake configuration is incomplete; reconfiguring llama.cpp");
    }
    Ok(SourceConfiguration { path, fingerprint })
}
