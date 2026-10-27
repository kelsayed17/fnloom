//! Fixtures shared by the unit tests.

use std::collections::BTreeMap;

use crate::graph::Graph;
use crate::node::{Dependency, InputSpec, NodeId, NodeSpec};
use crate::plan::Request;

/// A spec called `name` with `payload`, reading each `(input, dependency)` pair.
pub fn valued_spec<P>(name: &str, deps: &[(&str, Dependency)], payload: P) -> NodeSpec<P> {
    NodeSpec {
        name: name.to_owned(),
        inputs: deps
            .iter()
            .map(|&(input, dependency)| InputSpec {
                name: input.to_owned(),
                dependency,
            })
            .collect(),
        tags: BTreeMap::new(),
        payload,
    }
}

/// A spec called `name` reading each `(input, dependency)` pair.
pub fn spec(name: &str, deps: &[(&str, Dependency)]) -> NodeSpec<()> {
    valued_spec(name, deps, ())
}

/// A spec called `name` with `payload` that requires every one of `deps`.
pub fn valued<P>(name: &str, deps: &[&str], payload: P) -> NodeSpec<P> {
    let required: Vec<_> = deps.iter().map(|&d| (d, Dependency::Required)).collect();
    valued_spec(name, &required, payload)
}

/// A spec called `name` that requires every one of `deps`.
pub fn node(name: &str, deps: &[&str]) -> NodeSpec<()> {
    valued(name, deps, ())
}

/// Owned copies of `names`.
pub fn strings(names: &[&str]) -> Vec<String> {
    names.iter().map(|&name| name.to_owned()).collect()
}

/// A request for `outputs`, supplying `inputs` and overriding `overrides`.
pub fn request(outputs: &[&str], inputs: &[&str], overrides: &[&str]) -> Request {
    Request {
        outputs: strings(outputs),
        inputs: strings(inputs).into_iter().collect(),
        overrides: strings(overrides).into_iter().collect(),
    }
}

/// The names of `ids` in `graph`.
pub fn names<P>(graph: &Graph<P>, ids: &[NodeId]) -> Vec<String> {
    ids.iter().map(|&id| graph.node(id).name.clone()).collect()
}
