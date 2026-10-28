//! Key plans: the cache keys of a plan's computed nodes, chained from their sources' versions.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString};

use crate::fingerprint::Digest;
use crate::store::MemoryStore;

/// The version of a source that is neither in `versions` nor a computed node: an optional
/// input not supplied. It equals no fingerprint and no key but by a 2^-256 chance.
const ABSENT: Digest = *b"\x00fnloom: optional input absent\x00\x00";

/// One computed node as given: its name, its static key part, the names of the sources
/// its key reads, whether it is stored, and every source it reads.
type NodeEntry = (String, Option<Vec<u8>>, Vec<String>, bool, Vec<String>);

/// A source a key reads: its interned name, and its position when it is a computed node
/// (earlier in plan order).
struct Read {
    name: Py<PyString>,
    computed: Option<usize>,
}

/// One computed node of the plan.
struct KeyNode {
    name: Py<PyString>,
    /// `None` when the node cannot be cached.
    stat: Option<Digest>,
    reads: Vec<Read>,
    /// Whether a run looks the node up and stores it: its policy is `default`.
    stored: bool,
    /// The positions of the computed nodes it reads, for the walk back from the outputs.
    sources: Vec<usize>,
}

/// The cache keys of a plan's computed nodes, made once per prepared plan.
///
/// Each entry is one computed node, in plan order: its name, its static key part (32
/// bytes, or `None` when the node is uncacheable), the names of the nodes and inputs its
/// key reads, in declaration order, whether its policy is `default`, and every node and
/// input it reads. `outputs` are where the walk back starts.
///
/// A node's key is BLAKE3 keyed by its static part, over the versions of what it reads,
/// 32 bytes each. The static part already covers the key's parameter names and constants.
#[pyclass(frozen, module = "fnloom._core")]
pub struct KeyPlan {
    nodes: Vec<KeyNode>,
    /// Positions by name.
    index: HashMap<String, usize>,
    /// The computed outputs' positions.
    outputs: Vec<usize>,
}

#[pymethods]
impl KeyPlan {
    /// Builds a key plan; raises `ValueError` for a node listed twice, a key reading a
    /// node listed at or after its own, or a static part that is not 32 bytes.
    #[new]
    #[pyo3(signature = (nodes, outputs = Vec::new()))]
    fn new(py: Python<'_>, nodes: Vec<NodeEntry>, outputs: Vec<String>) -> PyResult<Self> {
        let mut index: HashMap<String, usize> = HashMap::with_capacity(nodes.len());
        for (position, (name, ..)) in nodes.iter().enumerate() {
            if index.insert(name.clone(), position).is_some() {
                return Err(PyValueError::new_err(format!(
                    "key plan lists node '{name}' twice"
                )));
            }
        }
        let mut built = Vec::with_capacity(nodes.len());
        for (position, (name, stat, reads, stored, sources)) in nodes.into_iter().enumerate() {
            let stat = stat
                .map(|stat| {
                    Digest::try_from(stat.as_slice()).map_err(|_| {
                        PyValueError::new_err(format!(
                            "key plan node '{name}' has a static part of {} bytes, not 32",
                            stat.len()
                        ))
                    })
                })
                .transpose()?;
            let reads = reads
                .into_iter()
                .map(|source| {
                    let computed = index.get(&source).copied();
                    if computed.is_some_and(|at| at >= position) {
                        return Err(PyValueError::new_err(format!(
                            "key plan node '{name}' reads '{source}' before it is computed"
                        )));
                    }
                    Ok(Read {
                        name: PyString::intern(py, &source).unbind(),
                        computed,
                    })
                })
                .collect::<PyResult<_>>()?;
            let sources = sources
                .iter()
                .filter_map(|source| index.get(source).copied())
                .collect();
            built.push(KeyNode {
                name: PyString::intern(py, &name).unbind(),
                stat,
                reads,
                stored,
                sources,
            });
        }
        let outputs = outputs
            .into_iter()
            .filter_map(|output| index.get(&output).copied())
            .collect();
        Ok(Self {
            nodes: built,
            index,
            outputs,
        })
    }

    /// The key of every computed node, by name; `versions` holds the version of every
    /// input, override and pre-run `recompute` node by name.
    ///
    /// A source's version is its entry in `versions` when it has one, even when it is
    /// also a computed node of this plan; else that computed node's own key; else the
    /// marker of an absent optional input. A key is `None` when its static part, or any
    /// version it reads, is `None`. Every node gets a key computed from its sources,
    /// including one named in `versions`: the caller ignores the keys of nodes it does
    /// not run.
    fn keys<'py>(&self, versions: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
        let py = versions.py();
        let keys = self.compute(versions)?;
        let result = PyDict::new(py);
        for (node, key) in self.nodes.iter().zip(&keys) {
            match key {
                Some(key) => result.set_item(node.name.bind(py), PyBytes::new(py, key))?,
                None => result.set_item(node.name.bind(py), py.None())?,
            }
        }
        Ok(result)
    }

    /// The keys, and the hits that stop the walk back from the outputs, in `store`.
    ///
    /// The walk, as `CacheRun._frontier` without a nested run: a stored node whose key
    /// `store` holds is a hit; every other computed node it reaches must run, and it goes
    /// on into what that node reads. Returns the keys, for `Plan.run` to store each
    /// stored node's result under as it succeeds, and the hits by name.
    fn walk<'py>(
        slf: &Bound<'py, Self>,
        versions: &Bound<'py, PyDict>,
        store: &Bound<'py, MemoryStore>,
    ) -> PyResult<(Keys, Bound<'py, PyDict>)> {
        let py = slf.py();
        let this = slf.get();
        let keys = this.compute(versions)?;
        let hits = PyDict::new(py);
        let store_ref = store.get();
        let mut seen = vec![false; this.nodes.len()];
        let mut stack = this.outputs.clone();
        while let Some(at) = stack.pop() {
            let Some(was_seen) = seen.get_mut(at) else {
                continue;
            };
            if *was_seen {
                continue;
            }
            *was_seen = true;
            let Some(node) = this.nodes.get(at) else {
                continue;
            };
            if node.stored
                && let Some(Some(key)) = keys.get(at)
                && let Some(value) = store_ref.load(py, key)
            {
                hits.set_item(node.name.bind(py), value)?;
                continue;
            }
            stack.extend_from_slice(&node.sources);
        }
        let pending = Keys {
            plan: slf.clone().unbind(),
            digests: keys,
            store: store.clone().unbind(),
        };
        Ok((pending, hits))
    }
}

impl KeyPlan {
    /// Every key, in plan order.
    fn compute(&self, versions: &Bound<'_, PyDict>) -> PyResult<Vec<Option<Digest>>> {
        // A computed node named in `versions` reads as that version: there are few
        // names in `versions`, so find those nodes once rather than look each read up.
        let mut given = vec![Given::No; self.nodes.len()];
        for (name, version) in versions.iter() {
            if let Ok(name) = name.cast::<PyString>()
                && let Some(&at) = self.index.get(name.to_str()?)
                && let Some(slot) = given.get_mut(at)
            {
                *slot = Given::Yes(version_of(&version)?);
            }
        }
        let mut keys: Vec<Option<Digest>> = Vec::with_capacity(self.nodes.len());
        let mut buffer: Vec<u8> = Vec::new();
        for node in &self.nodes {
            let key = node_key(node, versions, &given, &keys, &mut buffer)?;
            keys.push(key);
        }
        Ok(keys)
    }

    /// The position of the computed node `name`.
    fn position(&self, name: &str) -> Option<usize> {
        self.index.get(name).copied()
    }
}

/// Whether `versions` names a computed node, and with what version.
#[derive(Clone, Copy)]
enum Given {
    No,
    Yes(Option<Digest>),
}

/// A version from `versions`: `None`, or 32 bytes.
fn version_of(version: &Bound<'_, PyAny>) -> PyResult<Option<Digest>> {
    if version.is_none() {
        return Ok(None);
    }
    let bytes = version.cast::<PyBytes>()?.as_bytes();
    Digest::try_from(bytes)
        .map(Some)
        .map_err(|_| PyValueError::new_err(format!("a version is 32 bytes, not {}", bytes.len())))
}

/// The key of `node`, given `versions`, the computed nodes they name, and the keys of the
/// nodes before it.
fn node_key(
    node: &KeyNode,
    versions: &Bound<'_, PyDict>,
    given: &[Given],
    keys: &[Option<Digest>],
    buffer: &mut Vec<u8>,
) -> PyResult<Option<Digest>> {
    let Some(stat) = &node.stat else {
        return Ok(None);
    };
    let py = versions.py();
    buffer.clear();
    for read in &node.reads {
        let version = match read.computed {
            Some(at) => match given.get(at) {
                Some(Given::Yes(version)) => *version,
                _ => keys.get(at).copied().flatten(),
            },
            None => match versions.get_item(read.name.bind(py))? {
                Some(version) => version_of(&version)?,
                None => Some(ABSENT),
            },
        };
        let Some(version) = version else {
            return Ok(None);
        };
        buffer.extend_from_slice(&version);
    }
    Ok(Some(*blake3::keyed_hash(stat, buffer).as_bytes()))
}

/// One run's keys, from `KeyPlan.walk`, which `Plan.run` takes as `on_value` to store
/// each stored node's result in the memory store as it succeeds, with no Python call.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Keys {
    plan: Py<KeyPlan>,
    digests: Vec<Option<Digest>>,
    store: Py<MemoryStore>,
}

impl Keys {
    /// Stores `value`, the result of the computed node `name`, when it is stored.
    pub fn store(&self, py: Python<'_>, name: &str, value: &Py<PyAny>) -> PyResult<()> {
        let plan = self.plan.get();
        let Some(at) = plan.position(name) else {
            return Ok(());
        };
        let stored = plan.nodes.get(at).is_some_and(|node| node.stored);
        match self.digests.get(at) {
            Some(Some(key)) if stored => self.store.get().store(py, key, value.clone_ref(py)),
            _ => Ok(()),
        }
    }
}
