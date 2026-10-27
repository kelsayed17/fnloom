//! fnloom-core: the node IR, plan compiler and executor of the fnloom dataflow engine.
//! See docs/ for the contract this crate implements.
// Declared here, not in `[lints]`, because a crate that inherits the workspace
// lint table cannot also override one level in its own manifest.
#![forbid(unsafe_code)]

pub mod error;
pub mod graph;
pub mod node;
pub mod plan;
pub mod run;

#[cfg(test)]
mod testing;
