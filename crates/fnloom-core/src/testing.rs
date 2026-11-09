//! Fixtures shared by the unit tests.

use std::collections::BTreeMap;

use crate::graph::Graph;
use crate::node::{Collect, Dependency, InputSpec, NodeId, NodeSpec};
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
                collect: None,
            })
            .collect(),
        tags: BTreeMap::new(),
        payload,
        declines: false,
        expands: false,
    }
}

/// A spec like `valued_spec` whose node may decline: its value may be absent.
pub fn declining<P>(name: &str, deps: &[(&str, Dependency)], payload: P) -> NodeSpec<P> {
    NodeSpec {
        declines: true,
        expands: false,
        ..valued_spec(name, deps, payload)
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

/// How a test spec reads one input: its name, its dependency, and whether it collects.
pub type Read<'a> = (&'a str, Dependency, Option<Collect>);

/// A required, ordinary read of `name`.
pub const fn plain(name: &str) -> Read<'_> {
    (name, Dependency::Required, None)
}

/// A required collect of `name` that keeps the present branches.
pub const fn collect(name: &str) -> Read<'_> {
    (name, Dependency::Required, Some(Collect::Present))
}

/// A spec called `name` with `payload`, reading each of `reads`.
pub fn reading<P>(name: &str, reads: &[Read<'_>], payload: P) -> NodeSpec<P> {
    NodeSpec {
        inputs: reads
            .iter()
            .map(|&(input, dependency, collect)| InputSpec {
                name: input.to_owned(),
                dependency,
                collect,
            })
            .collect(),
        ..valued_spec(name, &[], payload)
    }
}

/// A spec like `reading` whose node is an expand.
pub fn expanding<P>(name: &str, reads: &[Read<'_>], payload: P) -> NodeSpec<P> {
    NodeSpec {
        expands: true,
        ..reading(name, reads, payload)
    }
}
