//! Embedding fnloom-core: a graph whose nodes add a constant to the sum of their inputs, planned
//! once and run for two input values.

use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::error::Error;

use fnloom_core::graph::Graph;
use fnloom_core::node::{Dependency, InputSpec, Node, NodeSpec};
use fnloom_core::plan::Request;
use fnloom_core::run::{Arg, Evaluate};

/// Runs a node: its payload, a constant, plus the sum of its arguments.
struct Adder;

impl Evaluate<i64> for Adder {
    type Value = i64;
    type Error = Infallible;

    fn evaluate(
        &mut self,
        _node: &Node<i64>,
        constant: &i64,
        args: &[Arg<'_, i64>],
    ) -> Result<i64, Infallible> {
        Ok(constant + args.iter().map(|arg| arg.value).sum::<i64>())
    }
}

/// A node called `name` that reads `reads` and adds `constant`.
fn node(name: &str, reads: &[&str], constant: i64) -> NodeSpec<i64> {
    NodeSpec {
        name: name.to_owned(),
        inputs: reads
            .iter()
            .map(|&read| InputSpec {
                name: read.to_owned(),
                dependency: Dependency::Required,
            })
            .collect(),
        tags: BTreeMap::new(),
        payload: constant,
    }
}

#[expect(clippy::print_stdout, reason = "an example shows its result")]
fn main() -> Result<(), Box<dyn Error>> {
    // `x` is read but declared by no spec, so the build makes it an input node.
    let graph = Graph::build(vec![
        node("a", &["x"], 1),
        node("b", &["a"], 10),
        node("c", &["a", "b"], 100),
    ])?;
    let request = Request {
        outputs: vec!["c".to_owned()],
        inputs: BTreeSet::from(["x".to_owned()]),
        overrides: BTreeSet::new(),
    };
    // Planned once: which nodes run, in what order, and when each value is released.
    let plan = graph.prepare(&request)?;
    for x in [1, 2] {
        let inputs = BTreeMap::from([("x".to_owned(), x)]);
        let outputs = plan.run(&graph, &mut Adder, &inputs, &BTreeMap::new(), &mut [])?;
        // a = 1 + x, b = 10 + a, c = 100 + a + b.
        assert_eq!(outputs.get("c"), Some(&(112 + 2 * x)));
        println!("x = {x}: c = {}", outputs["c"]);
    }
    Ok(())
}
