//! A driver's limits: the core's atomic caps, shared by every run of the driver.

use std::num::NonZeroUsize;
use std::sync::Arc;

use fnloom_core::limits::Limits as CoreLimits;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// How many tasks of a driver's runs may run at once.
///
/// `max_workers` caps the tasks that hold a worker slot, across every run of the driver;
/// `None` caps none.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Limits {
    core: Arc<CoreLimits>,
}

#[pymethods]
impl Limits {
    /// Caps of `max_workers` workers, at least 1, or none.
    #[new]
    #[pyo3(signature = (max_workers = None))]
    fn new(max_workers: Option<usize>) -> PyResult<Self> {
        let workers =
            match max_workers {
                None => None,
                Some(count) => Some(NonZeroUsize::new(count).ok_or_else(|| {
                    PyValueError::new_err("max_workers must be at least 1, not 0")
                })?),
            };
        Ok(Self {
            core: Arc::new(CoreLimits::new(workers, Vec::new(), Vec::new(), Vec::new())),
        })
    }
}

impl Limits {
    /// The core's caps.
    pub const fn core(&self) -> &Arc<CoreLimits> {
        &self.core
    }
}
