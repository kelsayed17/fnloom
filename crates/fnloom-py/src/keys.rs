//! Key plans: the cache keys of a plan's computed nodes, chained from their sources' versions.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString};

use crate::fingerprint::{Digest, Framer, Kind};

/// The version of a source that is neither in `versions` nor a computed node: an optional
/// input not supplied. No fingerprint is 7 bytes long, so it equals no version.
const ABSENT: &[u8] = b"\x00absent";

/// One computed node's name, static key part and key parameters, as given.
type NodeEntry = (String, Option<Vec<u8>>, Vec<ParamEntry>);

/// One key parameter as given: its name, the source it reads, a constant's version.
type ParamEntry = (String, Option<String>, Option<Vec<u8>>);

/// Where a key parameter's version comes from.
enum Source {
    /// A node or input read by name: from `versions` when named there, else from the
    /// key of the computed node at `computed` (earlier in plan order), else absent.
    Read {
        name: Py<PyString>,
        computed: Option<usize>,
    },
    /// A constant's version, `None` when it could not be fingerprinted.
    Constant(Option<Vec<u8>>),
}

/// One key parameter: its name's UTF-8 and its source.
struct Param {
    name: Vec<u8>,
    source: Source,
}

/// One computed node: its interned name, static key part, and key parameters.
struct KeyNode {
    name: Py<PyString>,
    /// `None` when the node cannot be cached.
    stat: Option<Vec<u8>>,
    params: Vec<Param>,
}

/// The cache keys of a plan's computed nodes, made once per prepared plan.
///
/// Each entry is one computed node, in plan order: its name, its static key part (`None`
/// when the node is uncacheable), and its key parameters in declaration order. A key
/// parameter is its name and then either the name of the node or input it reads, or
/// `None` and the version of a constant.
#[pyclass(frozen, module = "fnloom._core")]
pub struct KeyPlan {
    nodes: Vec<KeyNode>,
}

#[pymethods]
impl KeyPlan {
    /// Builds a key plan; raises `ValueError` for a node listed twice, or a parameter
    /// reading a node listed at or after its own.
    #[new]
    fn new(py: Python<'_>, nodes: Vec<NodeEntry>) -> PyResult<Self> {
        let mut index: HashMap<String, usize> = HashMap::with_capacity(nodes.len());
        for (position, (name, _, _)) in nodes.iter().enumerate() {
            if index.insert(name.clone(), position).is_some() {
                return Err(PyValueError::new_err(format!(
                    "key plan lists node '{name}' twice"
                )));
            }
        }
        let mut built = Vec::with_capacity(nodes.len());
        for (position, (name, stat, params)) in nodes.into_iter().enumerate() {
            let params = params
                .into_iter()
                .map(|(param, source, version)| {
                    let source = match source {
                        None => Source::Constant(version),
                        Some(source) => {
                            let computed = index.get(&source).copied();
                            if computed.is_some_and(|at| at >= position) {
                                return Err(PyValueError::new_err(format!(
                                    "key plan node '{name}' reads '{source}' before it is computed"
                                )));
                            }
                            Source::Read {
                                name: PyString::intern(py, &source).unbind(),
                                computed,
                            }
                        }
                    };
                    Ok(Param {
                        name: param.into_bytes(),
                        source,
                    })
                })
                .collect::<PyResult<_>>()?;
            built.push(KeyNode {
                name: PyString::intern(py, &name).unbind(),
                stat,
                params,
            });
        }
        Ok(Self { nodes: built })
    }

    /// The key of every computed node, by name; `versions` holds the version of every
    /// input, override and pre-run `recompute` node by name.
    ///
    /// A key is `hash_parts([static, *for each param: name, version])`. A source's
    /// version is its entry in `versions` when it has one, even when it is also a
    /// computed node of this plan; else that computed node's own key; else the marker of
    /// an absent optional input. A key is `None` when its static part, or any version it
    /// reads, is `None`. Every node gets a key computed from its parameters, including
    /// one named in `versions`: the caller ignores the keys of nodes it does not run.
    fn keys<'py>(&self, versions: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
        let py = versions.py();
        let mut keys: Vec<Option<Digest>> = Vec::with_capacity(self.nodes.len());
        let result = PyDict::new(py);
        for node in &self.nodes {
            let key = node_key(node, versions, &keys)?;
            keys.push(key);
            match key {
                Some(key) => result.set_item(node.name.bind(py), PyBytes::new(py, &key))?,
                None => result.set_item(node.name.bind(py), py.None())?,
            }
        }
        Ok(result)
    }
}

/// The key of `node`, given `versions` and the keys of the nodes before it.
fn node_key(
    node: &KeyNode,
    versions: &Bound<'_, PyDict>,
    keys: &[Option<Digest>],
) -> PyResult<Option<Digest>> {
    let Some(stat) = &node.stat else {
        return Ok(None);
    };
    let py = versions.py();
    let mut framer = Framer::new(Kind::Parts);
    framer.part(stat);
    for param in &node.params {
        framer.part(&param.name);
        match &param.source {
            Source::Constant(None) => return Ok(None),
            Source::Constant(Some(version)) => framer.part(version),
            Source::Read { name, computed } => {
                if let Some(version) = versions.get_item(name.bind(py))? {
                    if version.is_none() {
                        return Ok(None);
                    }
                    framer.part(version.cast::<PyBytes>()?.as_bytes());
                } else if let Some(at) = computed {
                    match keys.get(*at).copied().flatten() {
                        Some(key) => framer.part(&key),
                        None => return Ok(None),
                    }
                } else {
                    framer.part(ABSENT);
                }
            }
        }
    }
    Ok(Some(framer.finish()))
}
