//! The scheduler under stress: eight threads, worker, node and group caps, the in-flight
//! bound, colliding single-flight keys, and a leaf that fails on every other round.
//!
//! `FNLOOM_STRESS_ROUNDS` sets the rounds (default 10).

use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::num::NonZeroUsize;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use fnloom_core::graph::Graph;
use fnloom_core::limits::{Grant, Limits, Resource};
use fnloom_core::node::{Collect, Dependency, InputSpec, Node, NodeId, NodeSpec};
use fnloom_core::plan::{Plan, Request};
use fnloom_core::run::{Arg, Blocks, Evaluate, Item, Slot};
use fnloom_core::schedule::{
    ArgValue, Layout, Next, Options, Outcome, Report, Scheduler, Seat, Task, Work, probe,
};

/// An iterator's items, how far it got, whether a pull runs, and how often it was finished.
#[derive(Debug)]
struct Cursor {
    items: Vec<i64>,
    next: AtomicUsize,
    pulling: AtomicBool,
    finishes: AtomicUsize,
}

/// The values: a number, a list, or an iterator.
#[derive(Clone, Debug)]
enum Val {
    Num(i64),
    List(Vec<Val>),
    Iter(Arc<Cursor>),
}

impl PartialEq for Val {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Num(a), Self::Num(b)) => a == b,
            (Self::List(a), Self::List(b)) => a == b,
            _ => false,
        }
    }
}

fn num(value: &Val) -> i64 {
    match value {
        Val::Num(n) => *n,
        Val::List(values) => values.iter().map(num).sum(),
        Val::Iter(_) => 0,
    }
}

/// What one run saw: its iterators, its leaf calls, and finishes during a pull.
#[derive(Default)]
struct Seen {
    cursors: Mutex<Vec<Arc<Cursor>>>,
    leaves: AtomicUsize,
    /// The leaf call that fails; `usize::MAX` for none.
    fail_at: AtomicUsize,
    mid_pull: AtomicUsize,
    hits: AtomicUsize,
}

impl Seen {
    fn new(fail_at: usize) -> Self {
        let seen = Self::default();
        seen.fail_at.store(fail_at, Ordering::SeqCst);
        seen
    }

    fn iterate(&self, iterable: &Val) -> Val {
        let items = match iterable {
            Val::List(values) => values.iter().map(num).collect(),
            _ => Vec::new(),
        };
        let cursor = Arc::new(Cursor {
            items,
            next: AtomicUsize::new(0),
            pulling: AtomicBool::new(false),
            finishes: AtomicUsize::new(0),
        });
        self.cursors
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(Arc::clone(&cursor));
        Val::Iter(cursor)
    }

    fn finish(&self, iterator: &Val) -> Val {
        let Val::Iter(cursor) = iterator else {
            return Val::List(Vec::new());
        };
        if cursor.pulling.load(Ordering::SeqCst) {
            self.mid_pull.fetch_add(1, Ordering::SeqCst);
        }
        cursor.finishes.fetch_add(1, Ordering::SeqCst);
        Val::List(cursor.items.iter().copied().map(Val::Num).collect())
    }
}

fn next_item(iterator: &Val) -> Item<Val> {
    let Val::Iter(cursor) = iterator else {
        return Item::Done;
    };
    let at = cursor.next.fetch_add(1, Ordering::Relaxed);
    cursor
        .items
        .get(at)
        .map_or(Item::Done, |&item| Item::Next(Val::Num(item)))
}

/// What each node does.
#[derive(Clone, Copy, Debug)]
enum Op {
    /// The items `0..n`.
    Outer(i64),
    /// For an outer item `k`, the items `0..k % 3`.
    Inner,
    /// `inner + 10 * (outer % 2)`: equal for equal single-flight keys.
    Leaf,
    /// The sum of its one gathered argument.
    Sum,
    /// Its one gathered argument.
    Keep,
}

fn compute(op: Op, args: &[Val]) -> Val {
    match op {
        Op::Outer(n) => Val::List((0..n).map(Val::Num).collect()),
        Op::Inner => Val::List((0..num(&args[0]) % 3).map(Val::Num).collect()),
        Op::Leaf => Val::Num(num(&args[0]) + 10 * (num(&args[1]) % 2)),
        Op::Sum => Val::Num(num(&args[0])),
        Op::Keep => args[0].clone(),
    }
}

/// `Plan::run`'s evaluator over the same operations.
struct Sequential<'s>(&'s Seen);

impl Evaluate<Op> for Sequential<'_> {
    type Value = Val;
    type Error = Infallible;

    fn evaluate(
        &mut self,
        _node: &Node<Op>,
        op: &Op,
        args: &[Arg<'_, Val>],
    ) -> Result<Val, Infallible> {
        let values: Vec<Val> = args.iter().map(|arg| arg.value.clone()).collect();
        Ok(compute(*op, &values))
    }

    fn blocks(&mut self) -> Option<&mut dyn Blocks<Op, Val, Infallible>> {
        Some(self)
    }
}

impl Blocks<Op, Val, Infallible> for Sequential<'_> {
    fn iterate(
        &mut self,
        _node: &Node<Op>,
        iterable: &Val,
        _branch: &[usize],
    ) -> Result<Val, Infallible> {
        Ok(self.0.iterate(iterable))
    }

    fn next_item(&mut self, _node: &Node<Op>, iterator: &Val) -> Result<Item<Val>, Infallible> {
        Ok(next_item(iterator))
    }

    fn finish_items(
        &mut self,
        _node: &Node<Op>,
        iterator: &Val,
        _exhausted: bool,
    ) -> Result<Val, Infallible> {
        Ok(self.0.finish(iterator))
    }

    fn gather(
        &mut self,
        _consumer: &Node<Op>,
        _source: NodeId,
        values: Vec<Val>,
    ) -> Result<Val, Infallible> {
        Ok(Val::List(values))
    }
}

fn spec(name: &str, reads: &[(&str, bool)], op: Op) -> NodeSpec<Op> {
    NodeSpec {
        name: name.to_owned(),
        inputs: reads
            .iter()
            .map(|&(input, collects)| InputSpec {
                name: input.to_owned(),
                dependency: Dependency::Required,
                collect: collects.then_some(Collect::Present),
            })
            .collect(),
        tags: BTreeMap::new(),
        payload: op,
        declines: false,
        expands: matches!(op, Op::Outer(_) | Op::Inner),
    }
}

fn nested(n: i64) -> Graph<Op> {
    Graph::build(vec![
        spec("outer", &[], Op::Outer(n)),
        spec("inner", &[("outer", false)], Op::Inner),
        spec("leaf", &[("inner", false), ("outer", false)], Op::Leaf),
        spec("inner_sum", &[("leaf", true)], Op::Sum),
        spec("outer_sum", &[("inner_sum", true)], Op::Keep),
    ])
    .unwrap_or_else(|error| unreachable!("the graph builds: {error}"))
}

/// Up to 50 µs, by the task's node and key.
fn jitter(task: &Task<Val>) -> Duration {
    let mix = task.key.as_slice().iter().fold(task.node.0, |hash, &part| {
        hash.wrapping_mul(31).wrapping_add(part)
    });
    Duration::from_micros(u64::from(mix.wrapping_mul(2_654_435_761) % 50))
}

/// Runs one task. A leaf's single-flight key is its outer item's parity and its inner item,
/// so many branches share one.
fn perform(
    plan: &Plan,
    graph: &Graph<Op>,
    seen: &Seen,
    task: &Task<Val>,
) -> Outcome<Val, String, String> {
    thread::sleep(jitter(task));
    let node = graph.node(task.node);
    match &task.work {
        Work::Compute { args, expands, .. } => {
            let values: Vec<Val> = args
                .iter()
                .filter_map(|(_, arg)| match arg {
                    ArgValue::One(value) => Some(value.clone()),
                    ArgValue::Gather(values) => Some(Val::List(values.clone())),
                    ArgValue::Omitted => None,
                })
                .collect();
            let Some(op) = node.payload() else {
                return Outcome::Failed(format!("{} has no payload", node.name));
            };
            if node.name == "leaf"
                && seen.leaves.fetch_add(1, Ordering::SeqCst) == seen.fail_at.load(Ordering::SeqCst)
            {
                return Outcome::Failed(format!("leaf {:?} failed", task.branch));
            }
            let value = compute(*op, &values);
            if *expands {
                Outcome::Iterator(seen.iterate(&value))
            } else {
                Outcome::Value {
                    value,
                    absent: false,
                }
            }
        }
        Work::Iterate { iterable } => Outcome::Iterator(seen.iterate(iterable)),
        Work::Pull { iterator } => {
            let Val::Iter(cursor) = iterator else {
                return Outcome::Failed("not an iterator".to_owned());
            };
            assert!(
                !cursor.pulling.swap(true, Ordering::SeqCst),
                "two pulls of one iterator at once"
            );
            thread::sleep(jitter(task));
            let item = next_item(iterator);
            cursor.pulling.store(false, Ordering::SeqCst);
            Outcome::Item(item)
        }
        Work::Finish { iterator, .. } => Outcome::Finished(seen.finish(iterator)),
        Work::Probe { block } => {
            let branch = task.branch.clone();
            probe(plan, graph, *block, &mut Vec::new(), |id| {
                let key = (graph.node(id).name == "leaf").then(|| {
                    let inner = branch.get(1).copied().unwrap_or(0);
                    format!("{}:{inner}", branch[0] % 2)
                });
                Ok::<_, String>((None, key))
            })
        }
    }
}

type Shared = (Mutex<Option<Scheduler<Val, String, String>>>, Condvar);

fn lock(shared: &Shared) -> MutexGuard<'_, Option<Scheduler<Val, String, String>>> {
    shared.0.lock().unwrap_or_else(PoisonError::into_inner)
}

/// One worker: takes tasks until the run is done, before `deadline`.
fn work(
    plan: &Plan,
    graph: &Graph<Op>,
    grant: &dyn Grant,
    shared: &Shared,
    seen: &Seen,
    deadline: Instant,
) {
    let mut guard = lock(shared);
    let mut report = Report::default();
    loop {
        assert!(Instant::now() < deadline, "the run hung");
        let Some(scheduler) = guard.as_mut() else {
            return;
        };
        match scheduler.next(plan, graph, grant, Seat::Take) {
            Next::Task(task) => {
                drop(guard);
                let outcome = perform(plan, graph, seen, &task);
                guard = lock(shared);
                if let Some(scheduler) = guard.as_mut() {
                    scheduler.complete(plan, graph, grant, task.id, outcome, &mut report);
                }
                seen.hits.fetch_add(report.hits.len(), Ordering::SeqCst);
                shared.1.notify_all();
                drop(guard);
                report.clear();
                guard = lock(shared);
            }
            Next::Wait => {
                guard = shared
                    .1
                    .wait_timeout(guard, Duration::from_millis(10))
                    .unwrap_or_else(PoisonError::into_inner)
                    .0;
            }
            Next::Done => {
                shared.1.notify_all();
                return;
            }
        }
    }
}

/// One round: the scheduler on eight threads under `limits`, against `Plan::run`.
fn round(graph: &Graph<Op>, plan: &Plan, layout: &Arc<Layout>, limits: &Limits, fail_at: usize) {
    let reference = Seen::new(usize::MAX);
    let want = plan
        .run_by_id(graph, &mut Sequential(&reference), |_, _| None, &mut [])
        .unwrap_or_else(|error| unreachable!("the reference run succeeds: {error}"));
    let seen = Seen::new(fail_at);
    let seeded: Vec<Option<Slot<Val>>> = vec![None; graph.nodes().len()];
    let scheduler = Scheduler::new(
        plan,
        graph,
        Arc::clone(layout),
        seeded,
        Options { in_flight: 4 },
        &mut Report::default(),
    );
    let shared: Shared = (Mutex::new(Some(scheduler)), Condvar::new());
    let deadline = Instant::now() + Duration::from_secs(60);
    thread::scope(|scope| {
        for _ in 0..8 {
            scope.spawn(|| work(plan, graph, limits, &shared, &seen, deadline));
        }
    });
    let scheduler = lock(&shared)
        .take()
        .unwrap_or_else(|| unreachable!("the scheduler is still there"));
    let got = scheduler.finish(plan, graph);
    if fail_at == usize::MAX {
        assert_eq!(got.ok(), Some(want), "outputs differ from Plan::run's");
    } else {
        assert!(got.is_err(), "the leaf's failure is raised");
    }
    let held = [
        limits.held(Resource::Worker),
        limits.held(Resource::Node(graph.id_of("leaf").unwrap_or(NodeId(0)))),
        limits.held(Resource::Group(0)),
    ];
    assert_eq!(held, [0, 0, 0], "slots still held: worker, node, group");
    assert_eq!(
        seen.mid_pull.load(Ordering::SeqCst),
        0,
        "an iterator finished during a pull"
    );
    for cursor in seen
        .cursors
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .iter()
    {
        assert_eq!(
            cursor.finishes.load(Ordering::SeqCst),
            1,
            "an iterator not finished exactly once"
        );
    }
}

#[test]
fn eight_threads_under_caps_and_failures_never_hang_leak_or_misfinish() {
    let rounds: usize = std::env::var("FNLOOM_STRESS_ROUNDS")
        .ok()
        .and_then(|rounds| rounds.parse().ok())
        .unwrap_or(10);
    let graph = nested(300);
    let plan = graph
        .prepare(&Request {
            outputs: vec!["outer_sum".to_owned()],
            inputs: BTreeSet::new(),
            overrides: BTreeSet::new(),
        })
        .unwrap_or_else(|error| unreachable!("the request plans: {error}"));
    let layout = Arc::new(Layout::new(&plan, &graph));
    let id = |name: &str| {
        graph
            .id_of(name)
            .unwrap_or_else(|| unreachable!("the graph has {name}"))
    };
    let mut caps = vec![None; graph.nodes().len()];
    caps[id("leaf").index()] = NonZeroUsize::new(2);
    let mut groups = vec![None; graph.nodes().len()];
    groups[id("inner").index()] = Some(0);
    groups[id("leaf").index()] = Some(0);
    let limits = Limits::new(
        NonZeroUsize::new(5),
        caps,
        groups,
        vec![NonZeroUsize::new(3).unwrap_or(NonZeroUsize::MIN)],
    );
    for at in 0..rounds {
        // Every other round, one of the first 50 leaf calls fails.
        let fail_at = if at % 2 == 0 { usize::MAX } else { at * 7 % 50 };
        round(&graph, &plan, &layout, &limits, fail_at);
    }
}
