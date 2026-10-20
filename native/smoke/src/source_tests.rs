use super::{external_contract as external, source_contract as source};
use flate2::{write::GzEncoder, Compression};
use std::{fs, path::Path};

fn archive(path: &Path, entries: &[(&str, tar::EntryType, &[u8])]) {
    let mut tar = tar::Builder::new(GzEncoder::new(
        fs::File::create(path).unwrap(),
        Compression::default(),
    ));
    for (name, kind, content) in entries {
        let mut header = tar::Header::new_gnu();
        header.set_size(content.len() as u64);
        header.set_mode(0o755);
        header.set_entry_type(*kind);
        header.as_mut_bytes()[..100].fill(0);
        header.as_mut_bytes()[..name.len()].copy_from_slice(name.as_bytes());
        header.set_cksum();
        tar.append(&header, *content).unwrap();
    }
    tar.into_inner().unwrap().finish().unwrap();
}

fn fixture(root: &Path) -> (source::SourceLock, std::path::PathBuf, std::path::PathBuf) {
    let directory = root.join("local");
    fs::create_dir_all(directory.join("src")).unwrap();
    fs::write(directory.join("CMakeLists.txt"), b"fixture").unwrap();
    fs::write(directory.join("src/llama.cpp"), b"pinned source\n").unwrap();
    let revision = "1111111111111111111111111111111111111111";
    let prefix = format!("llama.cpp-{revision}");
    let file = root.join("source.tar.gz");
    archive(
        &file,
        &[
            (&format!("{prefix}/"), tar::EntryType::Directory, b""),
            (
                &format!("{prefix}/CMakeLists.txt"),
                tar::EntryType::Regular,
                b"fixture",
            ),
            (
                &format!("{prefix}/src/llama.cpp"),
                tar::EntryType::Regular,
                b"pinned source\n",
            ),
        ],
    );
    (
        source::SourceLock {
            revision: revision.into(),
            archive_sha256: external::sha256(&file).unwrap(),
            tree_sha256: source::tree_sha256(&directory).unwrap(),
        },
        directory,
        file,
    )
}

#[test]
fn local_and_legacy_sources_are_verified_without_modifying_git_or_cache() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, local, _) = fixture(temp.path());
    fs::write(local.join(".git"), b"gitdir: untouched\n").unwrap();
    let cache = temp.path().join("cache");
    for (directory, legacy) in [(Some(local.as_path()), None), (None, Some(local.as_path()))] {
        assert_eq!(
            source::select(&lock, &cache, directory, legacy, None, true).unwrap(),
            local.canonicalize().unwrap()
        );
        assert!(!cache.exists());
        assert_eq!(
            fs::read(local.join(".git")).unwrap(),
            b"gitdir: untouched\n"
        );
    }
    fs::write(local.join("src/llama.cpp"), b"modified\r\n").unwrap();
    let error = source::select(&lock, &cache, Some(&local), None, None, true)
        .unwrap_err()
        .to_string();
    assert!(error.contains("tree SHA-256 mismatch"));
    assert_eq!(
        fs::read(local.join("src/llama.cpp")).unwrap(),
        b"modified\r\n"
    );
    assert!(!cache.exists());
}

#[test]
fn source_offline_miss_cold_archive_warm_reuse_and_corruption() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, _, archive) = fixture(temp.path());
    let cache = temp.path().join("cache");
    let error = source::acquire(&lock, &cache, None, true)
        .unwrap_err()
        .to_string();
    assert!(
        error.contains(&lock.revision)
            && error.contains("downloads are disabled")
            && error.contains("LLAMA_CPP_SOURCE_DIR")
    );
    let root = source::acquire(&lock, &cache, Some(&archive), true).unwrap();
    fs::remove_file(archive).unwrap();
    assert_eq!(root, source::acquire(&lock, &cache, None, true).unwrap());
    fs::write(root.join("src/llama.cpp"), b"corrupt").unwrap();
    assert!(source::acquire(&lock, &cache, None, true)
        .unwrap_err()
        .to_string()
        .contains("tree SHA-256 mismatch"));
    assert_eq!(fs::read(root.join("src/llama.cpp")).unwrap(), b"corrupt");
    assert!(fs::read_dir(root.parent().unwrap())
        .unwrap()
        .all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".fetch-")));
}

#[test]
fn source_archive_digest_failure_never_publishes_partial_cache() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, _, file) = fixture(temp.path());
    fs::write(&file, b"wrong archive").unwrap();
    let cache = temp.path().join("cache");
    assert!(source::acquire(&lock, &cache, Some(&file), true)
        .unwrap_err()
        .to_string()
        .contains("archive SHA-256 mismatch"));
    let entry = cache.join(&lock.archive_sha256);
    assert!(!entry.join("source").exists());
    assert!(fs::read_dir(entry).unwrap().all(|entry| !entry
        .unwrap()
        .file_name()
        .to_string_lossy()
        .starts_with(".fetch-")));
}

#[test]
fn source_cache_rejects_git_metadata_even_when_the_source_files_match() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, _, file) = fixture(temp.path());
    let cache = temp.path().join("cache");
    let root = source::acquire(&lock, &cache, Some(&file), true).unwrap();
    fs::write(root.join(".git"), b"gitdir: unexpected").unwrap();
    assert!(source::acquire(&lock, &cache, None, true)
        .unwrap_err()
        .to_string()
        .contains("Git metadata is not allowed"));
}

#[test]
fn explicit_archive_can_bypass_a_dirty_legacy_checkout_without_changing_it() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, legacy, file) = fixture(temp.path());
    fs::write(legacy.join("src/llama.cpp"), b"user changes").unwrap();
    let root = source::select(
        &lock,
        &temp.path().join("cache"),
        None,
        Some(&legacy),
        Some(&file),
        true,
    )
    .unwrap();
    source::verify_tree(&lock, &root).unwrap();
    assert_eq!(
        fs::read(legacy.join("src/llama.cpp")).unwrap(),
        b"user changes"
    );
}

#[test]
fn concurrent_source_acquisition_is_locked_and_atomic() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, _, file) = fixture(temp.path());
    let cache = temp.path().join("cache");
    std::thread::scope(|scope| {
        let handles: Vec<_> = (0..4)
            .map(|_| scope.spawn(|| source::acquire(&lock, &cache, Some(&file), true).unwrap()))
            .collect();
        let paths: Vec<_> = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect();
        assert!(paths.iter().all(|path| path == &paths[0]));
    });
}

#[test]
fn source_extraction_rejects_unsafe_paths_links_metadata_and_duplicates() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, _, _) = fixture(temp.path());
    let prefix = format!("llama.cpp-{}", lock.revision);
    for (name, kind) in [
        (format!("{prefix}/../outside"), tar::EntryType::Regular),
        (format!("/{prefix}/outside"), tar::EntryType::Regular),
        (format!("{prefix}/C:bad"), tar::EntryType::Regular),
        (format!("{prefix}/bad\\name"), tar::EntryType::Regular),
        (format!("{prefix}/.git/config"), tar::EntryType::Regular),
        (format!("{prefix}/link"), tar::EntryType::Symlink),
        (format!("{prefix}/hard"), tar::EntryType::Link),
        ("wrong-revision/file".into(), tar::EntryType::Regular),
        (format!("{prefix}/repeat"), tar::EntryType::Regular),
    ] {
        let attempt = tempfile::tempdir_in(temp.path()).unwrap();
        let file = attempt.path().join("bad.tar.gz");
        let entries = if name.ends_with("/repeat") {
            vec![(name.as_str(), kind, b"".as_slice()); 2]
        } else {
            vec![(name.as_str(), kind, b"".as_slice())]
        };
        archive(&file, &entries);
        assert!(
            source::extract(&lock, &file, &attempt.path().join("out")).is_err(),
            "{name}"
        );
    }
}

#[test]
fn extra_source_files_and_symlinks_are_not_trusted() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, local, _) = fixture(temp.path());
    fs::write(local.join("untracked.h"), b"extra").unwrap();
    assert!(source::verify_tree(&lock, &local).is_err());
    fs::remove_file(local.join("untracked.h")).unwrap();
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink("src/llama.cpp", local.join("link")).unwrap();
        assert!(source::verify_tree(&lock, &local)
            .unwrap_err()
            .to_string()
            .contains("non-regular"));
    }
}

#[test]
fn source_extractor_skips_bounded_global_pax_metadata() {
    let temp = tempfile::tempdir().unwrap();
    let (lock, _, file) = fixture(temp.path());
    archive(
        &file,
        &[
            (
                "pax_global_header",
                tar::EntryType::XGlobalHeader,
                b"metadata",
            ),
            (
                &format!("llama.cpp-{}/file", lock.revision),
                tar::EntryType::Regular,
                b"source",
            ),
        ],
    );
    let out = temp.path().join("out");
    source::extract(&lock, &file, &out).unwrap();
    assert_eq!(fs::read_dir(&out).unwrap().count(), 1);
    assert_eq!(fs::read(out.join("file")).unwrap(), b"source");
    archive(
        &file,
        &[(
            "pax_global_header",
            tar::EntryType::XGlobalHeader,
            &vec![b'x'; 65537],
        )],
    );
    assert!(
        source::extract(&lock, &file, &temp.path().join("too-large"))
            .unwrap_err()
            .to_string()
            .contains("PAX metadata exceeds")
    );
}

#[test]
#[ignore = "requires LLAMA_CPP_TEST_SOURCE_ARCHIVE with the real pinned upstream archive"]
fn real_pinned_source_archive_populates_and_reuses_offline_cache() {
    let archive = std::env::var_os("LLAMA_CPP_TEST_SOURCE_ARCHIVE")
        .expect("set LLAMA_CPP_TEST_SOURCE_ARCHIVE");
    let lock = source::source_lock().unwrap();
    let temp = tempfile::tempdir().unwrap();
    let root = source::acquire(&lock, temp.path(), Some(Path::new(&archive)), true).unwrap();
    assert_eq!(
        root,
        source::acquire(&lock, temp.path(), None, true).unwrap()
    );
    source::verify_tree(&lock, &root).unwrap();
}

#[test]
#[ignore = "requires LLAMA_CPP_TEST_SOURCE_DIR with an existing clean pinned checkout"]
fn real_pinned_local_checkout_is_used_read_only() {
    let directory =
        std::env::var_os("LLAMA_CPP_TEST_SOURCE_DIR").expect("set LLAMA_CPP_TEST_SOURCE_DIR");
    let lock = source::source_lock().unwrap();
    let temp = tempfile::tempdir().unwrap();
    let cache = temp.path().join("unused-cache");
    let root =
        source::select(&lock, &cache, Some(Path::new(&directory)), None, None, true).unwrap();
    assert_eq!(root, Path::new(&directory).canonicalize().unwrap());
    assert!(!cache.exists());
}
