//! Native SDK contract tests; no C++ compiler or model required.
#![allow(dead_code, unexpected_cfgs)]
#[path = "../../../llama-cpp-sys-2/build_support/external.rs"]
mod external_contract;
use external_contract as external;
#[path = "../../../llama-cpp-sys-2/build_support/automatic.rs"]
mod automatic_contract;
#[cfg(test)]
mod automatic_tests;
#[cfg(test)]
mod contract_tests;
#[path = "../../../llama-cpp-sys-2/build_support/source.rs"]
mod source_contract;
#[cfg(test)]
mod source_tests;
