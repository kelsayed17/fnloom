//! Python binding for fnloom-core, exposed as the extension module `fnloom._core`.
#![deny(unsafe_code)]

mod errors;
mod fingerprint;
mod graph;
mod keys;
mod observer;
mod plan;

use pyo3::prelude::*;

/// Returns the version of the native core, taken from the crate manifest.
#[pyfunction]
const fn core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Panics on purpose, so tests can check that a panic surfaces as a Python exception.
#[cfg(feature = "test-hooks")]
#[pyfunction]
#[expect(
    clippy::panic,
    reason = "the deliberate panic is this hook's whole purpose"
)]
fn _panic_for_test() {
    panic!("deliberate panic from fnloom._core._panic_for_test");
}

/// The `fnloom._core` extension module.
#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(core_version, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_panic_for_test, module)?)?;
    module.add_function(wrap_pyfunction!(fingerprint::fingerprint, module)?)?;
    module.add_function(wrap_pyfunction!(fingerprint::hash_parts, module)?)?;
    module.add(
        "UnfingerprintableError",
        module
            .py()
            .get_type::<fingerprint::UnfingerprintableError>(),
    )?;
    module.add_class::<graph::Graph>()?;
    module.add_class::<plan::Plan>()?;
    module.add_class::<keys::KeyPlan>()?;
    module.add_class::<observer::ObserverBase>()
}
