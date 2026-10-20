//! Resolve pinned source only after SDK selection has chosen a source build.
use super::{automatic, external};
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
const MAX_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_FILES: usize = 50000;

#[derive(Deserialize)]
pub struct SourceLock {
    pub revision: String,
    pub archive_sha256: String,
    pub tree_sha256: String,
}

pub fn source_lock() -> Result<SourceLock> {
    let lock: SourceLock = serde_json::from_str(include_str!("source-lock.json"))?;
    if lock.revision != external::REVISION
        || [&lock.archive_sha256, &lock.tree_sha256]
            .iter()
            .any(|value| value.len() != 64 || !value.bytes().all(|b| b.is_ascii_hexdigit()))
    {
        return Err(
            "source lock must match the SDK's fixed native revision and SHA-256 pins".into(),
        );
    }
    Ok(lock)
}

fn safe_path(path: &Path) -> Result<()> {
    if path.as_os_str().is_empty() {
        return Err("empty source path".into());
    }
    for part in path.components() {
        if let Component::Normal(name) = part {
            let name = name.to_str().ok_or("non UTF-8 source path")?;
            if !name.contains(['\\', ':', '\n', '\r']) {
                continue;
            }
        }
        return Err(format!("unsafe source path: {:?}", path.as_os_str()).into());
    }
    Ok(())
}

fn exists(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

pub fn tree_sha256(root: &Path) -> Result<String> {
    if !fs::symlink_metadata(root)?.is_dir() {
        return Err(format!("source root is not a regular directory: {}", root.display()).into());
    }
    let mut files = BTreeMap::new();
    let mut bytes = 0u64;
    for entry in walkdir::WalkDir::new(root)
        .min_depth(1)
        .into_iter()
        .filter_entry(|entry| {
            // A legacy checkout's Git metadata is not source and is never modified.
            !(entry.depth() == 1 && entry.file_name() == ".git")
        })
    {
        let entry = entry?;
        if entry.file_type().is_dir() {
            continue;
        }
        if !entry.file_type().is_file() {
            return Err(format!("non-regular source file: {}", entry.path().display()).into());
        }
        bytes = bytes
            .checked_add(entry.metadata()?.len())
            .ok_or("source size overflow")?;
        if bytes > MAX_BYTES || files.len() >= MAX_FILES {
            return Err("source tree exceeds verification limits".into());
        }
        let relative = entry.path().strip_prefix(root)?;
        // Components, not host separators, define the portable digest.
        safe_path(relative)?;
        let name = relative
            .components()
            .map(|part| part.as_os_str().to_str().unwrap())
            .collect::<Vec<_>>()
            .join("/");
        files.insert(name, external::sha256(entry.path())?);
    }
    let mut hash = Sha256::new();
    for (name, digest) in files {
        hash.update((name.len() as u64).to_be_bytes());
        hash.update(name.as_bytes());
        hash.update(digest.as_bytes());
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub fn verify_tree(lock: &SourceLock, root: &Path) -> Result<()> {
    if tree_sha256(root)? != lock.tree_sha256 {
        return Err(format!(
            "source tree SHA-256 mismatch at {}; expected exact llama.cpp {} (including files and line endings); use a clean matching LLAMA_CPP_SOURCE_DIR or the pinned LLAMA_CPP_SOURCE_ARCHIVE; existing files were not changed",
            root.display(), lock.revision
        ).into());
    }
    Ok(())
}

fn verify_cache(lock: &SourceLock, root: &Path) -> Result<()> {
    if exists(&root.join(".git"))? {
        return Err("Git metadata is not allowed in a source cache".into());
    }
    verify_tree(lock, root)
}

pub fn extract(lock: &SourceLock, archive: &Path, destination: &Path) -> Result<()> {
    let prefix = format!("llama.cpp-{}", lock.revision);
    let mut archive = tar::Archive::new(GzDecoder::new(fs::File::open(archive)?));
    let mut names = BTreeSet::new();
    let mut bytes = 0u64;
    let mut entries = 0usize;
    for entry in archive.entries()? {
        let mut entry = entry?;
        entries += 1;
        bytes = bytes
            .checked_add(entry.size())
            .ok_or("source archive size overflow")?;
        if entries > MAX_FILES || bytes > MAX_BYTES {
            return Err("source archive exceeds extraction limits".into());
        }
        // GitHub archives carry the commit in a global PAX header. It is
        // metadata, not a file, and must never be extracted into the tree.
        if entry.header().entry_type().is_pax_global_extensions() {
            if entry.size() > 65536 {
                return Err("source archive PAX metadata exceeds extraction limits".into());
            }
            continue;
        }
        if entry.path_bytes().iter().any(|b| b"\\:\n\r".contains(b)) {
            return Err("unsafe source archive path".into());
        }
        let path = entry.path()?.into_owned();
        safe_path(&path)?;
        let mut parts = path.components();
        if parts.next().map(|p| p.as_os_str()) != Some(std::ffi::OsStr::new(&prefix)) {
            return Err(format!(
                "source archive has the wrong revision prefix: {}",
                path.display()
            )
            .into());
        }
        let relative = parts.as_path();
        if relative.as_os_str().is_empty() && entry.header().entry_type().is_dir() {
            continue;
        }
        safe_path(relative)?;
        if relative.components().next().unwrap().as_os_str() == ".git"
            || !names.insert(relative.to_path_buf())
            || names.len() > MAX_FILES
        {
            return Err(
                "source archive contains Git metadata, duplicates or too many entries".into(),
            );
        }
        let output = destination.join(relative);
        if entry.header().entry_type().is_dir() {
            fs::create_dir_all(output)?;
        } else if entry.header().entry_type().is_file() {
            fs::create_dir_all(output.parent().ok_or("source file has no parent")?)?;
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&output)?;
            io::copy(&mut entry, &mut file)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(
                    &output,
                    fs::Permissions::from_mode(0o644 | (entry.header().mode()? & 0o111)),
                )?;
            }
        } else {
            return Err("source archive contains a link or special file".into());
        }
    }
    Ok(())
}

pub fn acquire(
    lock: &SourceLock,
    cache: &Path,
    archive: Option<&Path>,
    offline: bool,
) -> Result<PathBuf> {
    let parent = cache.join(&lock.archive_sha256);
    fs::create_dir_all(&parent)?;
    let guard = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(parent.join(".lock"))?;
    guard.lock()?;
    let root = parent.join("source");
    if exists(&root)? {
        verify_cache(lock, &root)?;
        return Ok(root.canonicalize()?);
    }
    if offline && archive.is_none() {
        return Err(format!(
            "llama.cpp source {} is not cached at {} and downloads are disabled; fetch once online or supply a clean LLAMA_CPP_SOURCE_DIR / pinned LLAMA_CPP_SOURCE_ARCHIVE",
            lock.revision, root.display()
        ).into());
    }
    let temporary = tempfile::Builder::new()
        .prefix(".fetch-")
        .tempdir_in(&parent)?;
    let downloaded = temporary.path().join("source.tar.gz");
    let archive = if let Some(archive) = archive {
        archive
    } else {
        let url = format!(
            "https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{}",
            lock.revision
        );
        println!(
            "cargo:warning=Downloading pinned llama.cpp source {}",
            lock.revision
        );
        let status = Command::new(if cfg!(windows) { "curl.exe" } else { "curl" })
            .args([
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
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
            return Err(format!("download of pinned llama.cpp source {} failed; retry or supply LLAMA_CPP_SOURCE_ARCHIVE / LLAMA_CPP_SOURCE_DIR (no Git checkout was modified)", lock.revision).into());
        }
        &downloaded
    };
    if external::sha256(archive)? != lock.archive_sha256 {
        return Err(
            "source archive SHA-256 mismatch; refusing to compile unverified source".into(),
        );
    }
    let extracted = temporary.path().join("source");
    fs::create_dir(&extracted)?;
    extract(lock, archive, &extracted)?;
    verify_cache(lock, &extracted)?;
    fs::rename(extracted, &root)?;
    Ok(root.canonicalize()?)
}

pub fn select(
    lock: &SourceLock,
    cache: &Path,
    directory: Option<&Path>,
    legacy: Option<&Path>,
    archive: Option<&Path>,
    offline: bool,
) -> Result<PathBuf> {
    if let Some(directory) = directory {
        verify_tree(lock, directory)
            .map_err(|error| format!("LLAMA_CPP_SOURCE_DIR {}: {error}", directory.display()))?;
        return Ok(directory.canonicalize()?);
    }
    if archive.is_none() && !exists(&cache.join(&lock.archive_sha256).join("source"))? {
        if let Some(legacy) = legacy.filter(|path| path.join("CMakeLists.txt").is_file()) {
            verify_tree(lock, legacy)?;
            return Ok(legacy.canonicalize()?);
        }
    }
    acquire(lock, cache, archive, offline)
}

fn cache_directory() -> Result<PathBuf> {
    if let Some(path) = env::var_os("LLAMA_CPP_SOURCE_CACHE") {
        return Ok(path.into());
    }
    let cargo_home = env::var_os("CARGO_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            env::var_os("HOME")
                .or_else(|| env::var_os("USERPROFILE"))
                .map(|path| PathBuf::from(path).join(".cargo"))
        })
        .ok_or("cannot locate Cargo home; set LLAMA_CPP_SOURCE_CACHE")?;
    Ok(cargo_home.join("llama-cpp-sources"))
}

pub fn resolve() -> Result<PathBuf> {
    let lock = source_lock()?;
    let directory = env::var_os("LLAMA_CPP_SOURCE_DIR").map(PathBuf::from);
    let archive = env::var_os("LLAMA_CPP_SOURCE_ARCHIVE").map(PathBuf::from);
    if let Some(archive) = &archive {
        println!("cargo:rerun-if-changed={}", archive.display());
    }
    let legacy =
        PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").ok_or("missing CARGO_MANIFEST_DIR")?)
            .join("llama.cpp");
    let root = select(
        &lock,
        &cache_directory()?,
        directory.as_deref(),
        Some(&legacy),
        archive.as_deref(),
        automatic::offline(),
    )?;
    println!("cargo:source_dir={}", root.display());
    // Include directory membership, vendor headers and all other pinned files
    // in Cargo's freshness checks, not only the CMake/source subset below.
    println!("cargo:rerun-if-changed={}", root.display());
    println!("cargo:source_revision={}", lock.revision);
    println!(
        "cargo:warning=Using verified llama.cpp source {} at {}",
        lock.revision,
        root.display()
    );
    Ok(root)
}
