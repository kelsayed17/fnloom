//! Instruction-count benchmarks of the core: `Graph::build`, `Graph::prepare` and `Plan::run`,
//! each on chain, fan-in and layered graphs of 1,000 and 10,000 nodes.
//!
//! Wall-clock time is too noisy on shared CI runners to gate on, so the regression gate
//! compares instruction counts measured under callgrind (see `scripts/bench_gate.py`).
//!
//! Harness: `gungraun` 0.18.2 (licence `Apache-2.0 OR MIT`), the maintained successor of
//! `iai-callgrind` (same authors, same API; `iai-callgrind` stopped at 0.16.1). It is pinned
//! exactly, as a dev-dependency only, because 0.18.2 declares `rust-version = "1.74.1"`, which
//! builds on this workspace's MSRV of 1.85. Later releases need 1.85.1 (0.19) and 1.88 (0.20).
//! The matching runner is installed with `cargo install gungraun-runner --version 0.18.2
//! --locked`, and valgrind must be on the PATH. Both are Linux-only, so on other systems this
//! target compiles to a program that says it is skipped.
//!
//! Run: `cargo bench -p fnloom-core --bench core -- --output-format=json > bench.jsonl`, then
//! `uv run scripts/bench_gate.py --baseline crates/fnloom-core/benches/baseline.json
//! --results bench.jsonl`.
//!
//! The committed baseline holds exact counts, which depend on the CPU architecture, the
//! toolchain and the C library. It was generated on aarch64 Linux in the `rust:1.99.1` image
//! (valgrind 3.24 from its apt), and the CI job `instruction-counts` runs in that same image
//! on an arm64 runner. Regenerate it in that environment with `--write-baseline`, in the
//! commit that changes the cost on purpose.
//!
//! The shapes mirror `benches/generate.py`. Setup runs outside the measured region.

#[cfg(target_os = "linux")]
mod suite {
    //! The measured functions and their workloads.

    use std::collections::{BTreeMap, BTreeSet};
    use std::convert::Infallible;
    use std::hint::black_box;

    use fnloom_core::graph::Graph;
    use fnloom_core::node::{Dependency, InputSpec, Node, NodeSpec};
    use fnloom_core::plan::{Plan, Request};
    use fnloom_core::run::{Arg, Evaluate};
    use gungraun::prelude::*;

    /// Row width of the layered shape, as in `benches/generate.py`.
    const LAYER_WIDTH: usize = 10;

    /// The graph shapes of `benches/generate.py`.
    #[derive(Clone, Copy, Debug)]
    pub enum Shape {
        /// `n0` reads the input `seed`; each `n{i}` reads `n{i-1}`.
        Chain,
        /// One node `total` reading `n` inputs.
        FanIn,
        /// Rows of ten nodes, each reading two nodes of the row above.
        Layered,
    }

    /// Sums its arguments; the cheapest payload that still reads every edge.
    struct Sum;

    impl Evaluate<()> for Sum {
        type Value = u64;
        type Error = Infallible;

        fn evaluate(
            &mut self,
            _node: &Node<()>,
            _payload: &(),
            args: &[Arg<'_, u64>],
        ) -> Result<u64, Infallible> {
            Ok(args.iter().fold(0, |sum, arg| sum.wrapping_add(arg.value)))
        }
    }

    fn node(name: String, reads: Vec<String>) -> NodeSpec<()> {
        NodeSpec {
            name,
            inputs: reads
                .into_iter()
                .map(|name| InputSpec {
                    name,
                    dependency: Dependency::Required,
                })
                .collect(),
            tags: BTreeMap::new(),
            payload: (),
        }
    }

    fn chain(n: usize) -> Vec<NodeSpec<()>> {
        let first = node("n0".to_owned(), vec!["seed".to_owned()]);
        let rest = (1..n).map(|i| node(format!("n{i}"), vec![format!("n{}", i - 1)]));
        std::iter::once(first).chain(rest).collect()
    }

    fn fan_in(n: usize) -> Vec<NodeSpec<()>> {
        vec![node(
            "total".to_owned(),
            (0..n).map(|i| format!("x{i}")).collect(),
        )]
    }

    fn layered_node(i: usize) -> NodeSpec<()> {
        let (layer, column) = (i / LAYER_WIDTH, i % LAYER_WIDTH);
        if layer == 0 {
            return node(format!("l0_{column}"), vec!["seed".to_owned()]);
        }
        let left = format!("l{}_{column}", layer - 1);
        let right = format!("l{}_{}", layer - 1, (column + 1) % LAYER_WIDTH);
        node(format!("l{layer}_{column}"), vec![left, right])
    }

    fn specs(shape: Shape, n: usize) -> Vec<NodeSpec<()>> {
        match shape {
            Shape::Chain => chain(n),
            Shape::FanIn => fan_in(n),
            Shape::Layered => (0..n).map(layered_node).collect(),
        }
    }

    fn input_names(shape: Shape, n: usize) -> Vec<String> {
        match shape {
            Shape::FanIn => (0..n).map(|i| format!("x{i}")).collect(),
            Shape::Chain | Shape::Layered => vec!["seed".to_owned()],
        }
    }

    fn outputs(shape: Shape, n: usize) -> Vec<String> {
        match shape {
            Shape::Chain => vec![format!("n{}", n - 1)],
            Shape::FanIn => vec!["total".to_owned()],
            Shape::Layered => {
                let last_row = (n - 1) / LAYER_WIDTH;
                (0..LAYER_WIDTH)
                    .filter(|column| last_row * LAYER_WIDTH + column < n)
                    .map(|column| format!("l{last_row}_{column}"))
                    .collect()
            }
        }
    }

    fn request(shape: Shape, n: usize) -> Request {
        Request {
            outputs: outputs(shape, n),
            inputs: input_names(shape, n).into_iter().collect::<BTreeSet<_>>(),
            overrides: BTreeSet::new(),
        }
    }

    fn input_values(shape: Shape, n: usize) -> BTreeMap<String, u64> {
        input_names(shape, n).into_iter().zip(0..).collect()
    }

    #[expect(
        clippy::expect_used,
        reason = "a workload that does not build is a bug in this file, and aborting is right"
    )]
    fn graph(shape: Shape, n: usize) -> Graph<()> {
        Graph::build(specs(shape, n)).expect("generated specs form a valid graph")
    }

    #[expect(
        clippy::expect_used,
        reason = "a workload that does not plan is a bug in this file, and aborting is right"
    )]
    fn planned(shape: Shape, n: usize) -> (Graph<()>, Plan, BTreeMap<String, u64>) {
        let graph = graph(shape, n);
        let plan = graph
            .prepare(&request(shape, n))
            .expect("the request names nodes of the generated graph");
        (graph, plan, input_values(shape, n))
    }

    fn prepared(shape: Shape, n: usize) -> (Graph<()>, Request) {
        (graph(shape, n), request(shape, n))
    }

    // Each benchmark returns what it built, so the drop of a graph is not measured.

    #[library_benchmark]
    #[bench::chain_1000(args = (Shape::Chain, 1_000), setup = specs)]
    #[bench::chain_10000(args = (Shape::Chain, 10_000), setup = specs)]
    #[bench::fan_in_1000(args = (Shape::FanIn, 1_000), setup = specs)]
    #[bench::fan_in_10000(args = (Shape::FanIn, 10_000), setup = specs)]
    #[bench::layered_1000(args = (Shape::Layered, 1_000), setup = specs)]
    #[bench::layered_10000(args = (Shape::Layered, 10_000), setup = specs)]
    fn build(specs: Vec<NodeSpec<()>>) -> impl Sized {
        Graph::build(black_box(specs))
    }

    #[library_benchmark]
    #[bench::chain_1000(args = (Shape::Chain, 1_000), setup = prepared)]
    #[bench::chain_10000(args = (Shape::Chain, 10_000), setup = prepared)]
    #[bench::fan_in_1000(args = (Shape::FanIn, 1_000), setup = prepared)]
    #[bench::fan_in_10000(args = (Shape::FanIn, 10_000), setup = prepared)]
    #[bench::layered_1000(args = (Shape::Layered, 1_000), setup = prepared)]
    #[bench::layered_10000(args = (Shape::Layered, 10_000), setup = prepared)]
    fn prepare((graph, request): (Graph<()>, Request)) -> impl Sized {
        let plan = graph.prepare(black_box(&request));
        (graph, plan)
    }

    #[library_benchmark]
    #[bench::chain_1000(args = (Shape::Chain, 1_000), setup = planned)]
    #[bench::chain_10000(args = (Shape::Chain, 10_000), setup = planned)]
    #[bench::fan_in_1000(args = (Shape::FanIn, 1_000), setup = planned)]
    #[bench::fan_in_10000(args = (Shape::FanIn, 10_000), setup = planned)]
    #[bench::layered_1000(args = (Shape::Layered, 1_000), setup = planned)]
    #[bench::layered_10000(args = (Shape::Layered, 10_000), setup = planned)]
    fn run((graph, plan, inputs): (Graph<()>, Plan, BTreeMap<String, u64>)) -> impl Sized {
        let values = plan.run(
            &graph,
            &mut Sum,
            black_box(&inputs),
            &BTreeMap::new(),
            &mut [],
        );
        (graph, plan, values)
    }

    library_benchmark_group!(name = engine; benchmarks = build, prepare, run);
}

#[cfg(target_os = "linux")]
use suite::engine;

#[cfg(target_os = "linux")]
gungraun::main!(library_benchmark_groups = engine);

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("skipping the fnloom-core instruction-count benches: callgrind needs Linux");
}
