fn default_fixture() -> Result<automatic::RuntimeLock, serde_json::Error> {
    serde_json::from_str(include_str!("../fixtures/runtime-lock.json"))
}
use super::{automatic_contract as automatic, external_contract as external};
use flate2::{write::GzEncoder, Compression};
use serde_json::json;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};

fn mac_features() -> BTreeSet<String> {
    ["common".into(), "metal".into()].into()
}

fn variables(values: &[(&str, &str)]) -> BTreeMap<String, String> {
    values
        .iter()
        .map(|(key, value)| ((*key).into(), (*value).into()))
        .collect()
}

#[test]
fn unified_release_selects_all_eight_variants_without_gpu_probing() {
    let lock = default_fixture().unwrap();
    assert_eq!(lock.schema, 2);
    assert_eq!(lock.targets.values().map(|v| v.len()).sum::<usize>(), 8);
    for (target, variants) in &lock.targets {
        for (variant, bundle) in variants {
            let (selected, entry) =
                automatic::select_runtime(&lock, target, &bundle.features, &variables(&[]))
                    .unwrap();
            assert_eq!(selected, variant);
            assert_eq!(
                entry.archive,
                format!(
                    "llama-cpp-runtime-{}-{target}-{variant}.tar.gz",
                    lock.release
                )
            );
            if target.ends_with("linux-gnu") {
                assert_eq!(entry.cmake["LLAMA_SUBPROCESS"], "OFF");
            }
        }
    }
    let arm = &lock.targets["aarch64-pc-windows-msvc"]["cpu"];
    assert!(arm.cmake["CMAKE_CXX_FLAGS"]
        .split_whitespace()
        .any(|flag| flag == "/EHsc"));
    assert!(
        automatic::select_runtime(&lock, "unknown", &mac_features(), &variables(&[])).is_none()
    );
}

#[test]
fn published_lock_never_reuses_old_abi() {
    let lock = automatic::runtime_lock().unwrap();
    assert!(lock.release.contains(super::external::CRATE_VERSION));
    for variants in lock.targets.values() {
        for bundle in variants.values() {
            assert!(bundle.archive.contains(super::external::CRATE_VERSION));
        }
    }
}

#[test]
fn cpu_and_cuda_selection_remain_distinct_and_native_options_strict() {
    let lock = default_fixture().unwrap();
    for target in [
        "x86_64-unknown-linux-gnu",
        "x86_64-pc-windows-msvc",
        "aarch64-pc-windows-msvc",
    ] {
        let features = BTreeSet::from(["common".into()]);
        assert_eq!(
            automatic::select_runtime(&lock, target, &features, &variables(&[]))
                .unwrap()
                .0,
            "cpu"
        );
        for override_ in [
            ("LLAMA_BUILD_SHARED_LIBS", "1"),
            ("LLAMA_STATIC_CRT", "1"),
            ("GGML_OPENMP", "ON"),
            ("GGML_CPU_REPACK", "OFF"),
            ("CXXFLAGS", "-ffast-math"),
            ("CXX", "clang-cl"),
            ("CMAKE_GENERATOR", "Ninja"),
            ("LLAMA_LIB_PROFILE", "Debug"),
        ] {
            assert!(
                automatic::select_runtime(&lock, target, &features, &variables(&[override_]))
                    .is_none(),
                "{override_:?}"
            );
        }
        for extra in ["cuda", "dynamic-link", "dynamic-backends", "openmp"] {
            let mut different = features.clone();
            different.insert(extra.into());
            assert!(
                automatic::select_runtime(&lock, target, &different, &variables(&[])).is_none()
            );
        }
        check_archive_cache(target, &lock.release, json!(["common"]));
    }
}

#[test]
fn ambiguous_variants_are_rejected() {
    let mut value: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/runtime-lock.json")).unwrap();
    value["targets"]["aarch64-apple-darwin"]["duplicate"] =
        value["targets"]["aarch64-apple-darwin"]["metal"].clone();
    let lock: automatic::RuntimeLock = serde_json::from_value(value).unwrap();
    assert!(automatic::select_runtime(
        &lock,
        "aarch64-apple-darwin",
        &mac_features(),
        &variables(&[])
    )
    .is_none());
}

#[test]
fn matching_defaults_and_explicit_native_options_reuse_sdk() {
    let lock = default_fixture().unwrap();
    let bundle = &lock.targets["aarch64-apple-darwin"]["metal"];
    for values in [
        variables(&[]),
        variables(&[
            ("GGML_CPU_REPACK", "ON"),
            ("GGML_OPENMP", "false"),
            ("CMAKE_BUILD_TYPE", "Release"),
            ("CMAKE_BUILD_PARALLEL_LEVEL", "2"),
            ("LLAMA_BUILD_SHARED_LIBS", "0"),
        ]),
    ] {
        assert!(automatic::mismatches(bundle, &mac_features(), &values).is_empty());
    }
}

#[test]
fn native_parameter_and_feature_differences_select_source() {
    let lock = default_fixture().unwrap();
    let bundle = &lock.targets["aarch64-apple-darwin"]["metal"];
    for values in [
        variables(&[("GGML_CPU_REPACK", "OFF")]),
        variables(&[("GGML_FUTURE_OPTION", "ON")]),
        variables(&[("CXXFLAGS", "-ffast-math")]),
        variables(&[("CC_aarch64_apple_darwin", "clang")]),
        variables(&[("MACOSX_DEPLOYMENT_TARGET", "15.0")]),
        variables(&[("LLAMA_LIB_PROFILE", "Debug")]),
    ] {
        assert!(
            !automatic::mismatches(bundle, &mac_features(), &values).is_empty(),
            "{values:?}"
        );
    }
    let mut features = mac_features();
    features.insert("openmp".into());
    assert!(!automatic::mismatches(bundle, &features, &BTreeMap::new()).is_empty());
}

#[test]
fn rust_application_options_and_profiles_reuse_release_native_sdk() {
    let lock = default_fixture().unwrap();
    let bundle = &lock.targets["aarch64-apple-darwin"]["metal"];
    let values = variables(&[
        ("CARGO_ENCODED_RUSTFLAGS", "-C\u{1f}debuginfo=1"),
        ("CARGO_PROFILE_RELEASE_DEBUG", "true"),
        ("RUST_LOG", "debug"),
        ("LLAMA_CPP_SOURCE_DIR", "/not-needed-for-sdk"),
        ("LLAMA_CPP_SOURCE_CACHE", "/not-needed-for-sdk"),
        ("LLAMA_CPP_SOURCE_ARCHIVE", "/not-needed-for-sdk"),
    ]);
    assert!(automatic::mismatches(bundle, &mac_features(), &values).is_empty());
    for flag in [
        "-C\u{1f}target-cpu=native",
        "-Ctarget-feature=+avx2",
        "-Ctarget-feature=+crt-static",
    ] {
        assert!(!automatic::mismatches(
            bundle,
            &mac_features(),
            &variables(&[("CARGO_ENCODED_RUSTFLAGS", flag)])
        )
        .is_empty());
    }
}

#[test]
fn cuda_architecture_lists_and_upstream_boolean_semantics_are_preserved() {
    let lock = default_fixture().unwrap();
    let bundle = &lock.targets["x86_64-pc-windows-msvc"]["cuda"];
    assert!(automatic::mismatches(
        bundle,
        &bundle.features,
        &variables(&[
            (
                "CMAKE_CUDA_ARCHITECTURES",
                &bundle.cmake["CMAKE_CUDA_ARCHITECTURES"]
            ),
            ("LLAMA_BUILD_SHARED_LIBS", "1")
        ])
    )
    .is_empty());
    for values in [
        variables(&[("CMAKE_CUDA_ARCHITECTURES", "75")]),
        variables(&[("LLAMA_BUILD_SHARED_LIBS", "ON")]),
        variables(&[("LLAMA_STATIC_CRT", "1")]),
    ] {
        assert!(!automatic::mismatches(bundle, &bundle.features, &values).is_empty());
    }
}

fn write_archive(path: &Path, entries: &[(&str, tar::EntryType, &[u8])]) {
    let gzip = GzEncoder::new(fs::File::create(path).unwrap(), Compression::default());
    let mut builder = tar::Builder::new(gzip);
    for (name, kind, contents) in entries {
        let mut header = tar::Header::new_gnu();
        header.set_mode(0o644);
        header.set_size(contents.len() as u64);
        header.set_entry_type(*kind);
        // Deliberately bypass set_path's validation for traversal fixtures.
        header.as_mut_bytes()[..100].fill(0);
        header.as_mut_bytes()[..name.len()].copy_from_slice(name.as_bytes());
        header.set_cksum();
        builder.append(&header, *contents).unwrap();
    }
    builder.into_inner().unwrap().finish().unwrap();
}

#[test]
fn extractor_rejects_traversal_links_and_duplicate_entries() {
    for (name, kind, duplicate) in [
        ("../escape", tar::EntryType::Regular, false),
        ("/absolute", tar::EntryType::Regular, false),
        ("lib/link", tar::EntryType::Symlink, false),
        ("lib/hard", tar::EntryType::Link, false),
        ("C:\\escape", tar::EntryType::Regular, false),
        ("same", tar::EntryType::Regular, true),
    ] {
        let temporary = tempfile::tempdir().unwrap();
        let archive = temporary.path().join("bad.tar.gz");
        let mut entries = vec![(name, kind, b"".as_slice())];
        if duplicate {
            entries.push((name, kind, b"".as_slice()));
        }
        write_archive(&archive, &entries);
        assert!(
            automatic::extract(&archive, &temporary.path().join("out")).is_err(),
            "{name}"
        );
    }
}

#[test]
fn offline_cache_miss_and_bad_archive_do_not_silently_build_source() {
    let lock = default_fixture().unwrap();
    let temporary = tempfile::tempdir().unwrap();
    let cache = temporary.path().join("cache");
    assert!(
        automatic::acquire(&lock, "aarch64-apple-darwin", "metal", &cache, None, true)
            .unwrap_err()
            .to_string()
            .contains("not cached")
    );
    let archive = temporary.path().join("wrong.tgz");
    fs::write(&archive, b"not the pinned asset").unwrap();
    assert!(automatic::acquire(
        &lock,
        "aarch64-apple-darwin",
        "metal",
        &cache,
        Some(&archive),
        true
    )
    .unwrap_err()
    .to_string()
    .contains("archive SHA-256 mismatch"));
}

#[test]
fn acquire_validates_cold_archive_warm_cache_and_corruption() {
    check_archive_cache(
        "aarch64-apple-darwin",
        "zvec-0.1.162-r1",
        json!(["common", "metal"]),
    );
}

fn check_archive_cache(target: &str, release: &str, features: serde_json::Value) {
    let temporary = tempfile::tempdir().unwrap();
    let binding = temporary.path().join("bindings.rs");
    fs::write(&binding, b"// fixture").unwrap();
    let manifest = serde_json::to_vec(&json!({
        "schema": 2, "bindings_sha256": super::external::bindings_sha256(), "release": release, "crate_version": "0.1.162",
        "llama_cpp_revision": "cd959bae99ff4bed22bef2604f45241c09bbb8c8",
        "target": target, "features": features,
        "linkage": "static", "link_libraries": ["c++"],
        "files": {"bindings.rs": external::sha256(&binding).unwrap()}
    }))
    .unwrap();
    let manifest_file = temporary.path().join("manifest.json");
    fs::write(&manifest_file, &manifest).unwrap();
    let archive = temporary.path().join("fixture.tgz");
    write_archive(
        &archive,
        &[
            ("bindings.rs", tar::EntryType::Regular, b"// fixture"),
            ("manifest.json", tar::EntryType::Regular, &manifest),
        ],
    );
    let lock: automatic::RuntimeLock = serde_json::from_value(json!({
        "schema": 2, "release": release, "targets": {(target): {"test": {
            "archive": "fixture.tgz", "sha256": external::sha256(&archive).unwrap(),
            "manifest_sha256": external::sha256(&manifest_file).unwrap(),
            "features": features, "cmake": {}
        }}}
    }))
    .unwrap();
    let requested_features = serde_json::from_value(features).unwrap();
    assert!(
        automatic::select_runtime(&lock, target, &requested_features, &variables(&[]),).is_some()
    );
    let cache = temporary.path().join("cache");
    let root = automatic::acquire(&lock, target, "test", &cache, Some(&archive), true).unwrap();
    assert_eq!(
        root,
        automatic::acquire(&lock, target, "test", &cache, None, true).unwrap()
    );
    fs::write(root.join("bindings.rs"), b"corrupt").unwrap();
    assert!(automatic::acquire(&lock, target, "test", &cache, None, true).is_err());
    assert!(fs::read_dir(root.parent().unwrap())
        .unwrap()
        .all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".fetch-")));
}

#[test]
fn source_cache_reconfigures_on_option_changes_and_failed_attempts() {
    let temporary = tempfile::tempdir().unwrap();
    let out = temporary.path();
    let build = out.join("build");
    fs::create_dir(&build).unwrap();
    let cache = build.join("CMakeCache.txt");
    let object = build.join("preserved.o");
    fs::write(&object, b"object").unwrap();
    fs::write(&cache, b"old cache").unwrap();
    fs::write(build.join("cmake_install.cmake"), b"install").unwrap();
    let before = variables(&[("GGML_CPU_REPACK", "OFF")]);
    let first =
        automatic::prepare_source_configuration(out, &mac_features(), &before, out).unwrap();
    assert!(!cache.exists());
    assert_eq!(fs::read(&object).unwrap(), b"object");
    fs::write(&cache, b"configured OFF").unwrap();
    first.commit().unwrap();
    automatic::prepare_source_configuration(out, &mac_features(), &before, out).unwrap();
    assert!(cache.exists());
    let after = variables(&[("GGML_CPU_REPACK", "ON")]);
    automatic::prepare_source_configuration(out, &mac_features(), &after, out).unwrap();
    assert!(!cache.exists());
    // Simulate a failed attempt that left a partial cache but never committed.
    fs::write(&cache, b"partial new cache").unwrap();
    let retry = automatic::prepare_source_configuration(out, &mac_features(), &after, out).unwrap();
    assert!(!cache.exists());
    fs::write(&cache, b"configured ON").unwrap();
    retry.commit().unwrap();
    automatic::prepare_source_configuration(out, &mac_features(), &after, out).unwrap();
    assert!(cache.exists());
    // Identical options with a different resolved source must also reconfigure.
    automatic::prepare_source_configuration(
        out,
        &mac_features(),
        &after,
        &out.join("other-source"),
    )
    .unwrap();
    assert!(!cache.exists());
}
