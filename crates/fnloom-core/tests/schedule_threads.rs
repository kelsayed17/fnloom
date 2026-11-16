//! The scheduler driven from eight `std` threads behind one lock, against `Plan::run`.

use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::num::NonZeroUsize;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use fnloom_core::graph::Graph;
use fnloom_core::limits::{Grant, Limits, Resource, Unlimited};
use fnloom_core::node::{Collect, Dependency, InputSpec, Node, NodeId, NodeSpec};
use fnloom_core::plan::{Plan, Request};
use fnloom_core::run::{Arg, Blocks, Evaluate, Item, Slot};
use fnloom_core::schedule::{
    ArgValue, Layout, Next, Options, Outcome, Report, Scheduler, Seat, Task, Work, probe,
};

/// An iterator's items and how far it got; shared, since a pull may run on any thread.
#[derive(Debug)]
struct Cursor {
    items: Vec<i64>,
    next: AtomicUsize,
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

/// What each node does.
#[derive(Clone, Copy, Debug)]
enum Op {
    /// The items `0..n`.
    Outer(i64),
    /// For an outer item `k`, the items `0..k % 3`.
    Inner,
    /// `10 * outer + inner`.
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
        Op::Leaf => Val::Num(num(&args[0]) + 10 * num(&args[1])),
        Op::Sum => Val::Num(num(&args[0])),
        Op::Keep => args[0].clone(),
    }
}

fn iterate(iterable: &Val) -> Val {
    let items = match iterable {
        Val::List(values) => values.iter().map(num).collect(),
        _ => Vec::new(),
    };
    Val::Iter(Arc::new(Cursor {
        items,
        next: AtomicUsize::new(0),
    }))
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

fn finish(iterator: &Val) -> Val {
    let Val::Iter(cursor) = iterator else {
        return Val::List(Vec::new());
    };
    Val::List(cursor.items.iter().copied().map(Val::Num).collect())
}

/// `Plan::run`'s evaluator over the same operations.
struct Sequential;

impl Evaluate<Op> for Sequential {
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

impl Blocks<Op, Val, Infallible> for Sequential {
    fn iterate(
        &mut self,
        _node: &Node<Op>,
        iterable: &Val,
        _branch: &[usize],
    ) -> Result<Val, Infallible> {
        Ok(iterate(iterable))
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
        Ok(finish(iterator))
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
        absent_on_error: false,
    }
}

/// `outer` yields `0..n`; `inner` yields `0..k % 3` for each outer item `k`; `leaf` is
/// `10 * outer + inner`; `inner_sum` sums `leaf` per outer branch, `outer_sum` collects it.
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

/// Up to 50 µs, by the task's node and key: a sleep that differs from task to task.
fn jitter(task: &Task<Val>) -> u64 {
    let mix = task.key.as_slice().iter().fold(task.node.0, |hash, &part| {
        hash.wrapping_mul(31).wrapping_add(part)
    });
    u64::from(mix.wrapping_mul(2_654_435_761) % 50)
}

/// Runs one task, after a short sleep that depends on it.
fn perform(plan: &Plan, graph: &Graph<Op>, task: &Task<Val>) -> Outcome<Val, String, String> {
    let micros = jitter(task);
    thread::sleep(Duration::from_micros(micros));
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
            let value = compute(*op, &values);
            if *expands {
                Outcome::Iterator(iterate(&value))
            } else {
                Outcome::Value {
                    value,
                    absent: false,
                }
            }
        }
        Work::Iterate { iterable } => Outcome::Iterator(iterate(iterable)),
        Work::Pull { iterator } => Outcome::Item(next_item(iterator)),
        Work::Finish { iterator, .. } => Outcome::Finished(finish(iterator)),
        Work::Probe { block } => probe(plan, graph, *block, &mut Vec::new(), |_| {
            Ok::<_, String>((None, None))
        }),
    }
}

type Shared = (Mutex<Option<Scheduler<Val, String, String>>>, Condvar);

fn lock(shared: &Shared) -> MutexGuard<'_, Option<Scheduler<Val, String, String>>> {
    shared.0.lock().unwrap_or_else(PoisonError::into_inner)
}

/// How many tasks, and how many of `leaf`, run now and ran at most at once.
#[derive(Default)]
struct Watch {
    now: AtomicUsize,
    most: AtomicUsize,
    leaves: AtomicUsize,
    most_leaves: AtomicUsize,
    ran: AtomicUsize,
}

fn enter(now: &AtomicUsize, most: &AtomicUsize) {
    let count = now.fetch_add(1, Ordering::AcqRel) + 1;
    most.fetch_max(count, Ordering::AcqRel);
}

/// One worker: takes tasks until the run is done, waiting at most `deadline` in all.
fn work(
    plan: &Plan,
    graph: &Graph<Op>,
    grant: &dyn Grant,
    shared: &Shared,
    deadline: Instant,
    watch: &Watch,
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
                let leaf = graph.node(task.node).name == "leaf";
                enter(&watch.now, &watch.most);
                if leaf {
                    enter(&watch.leaves, &watch.most_leaves);
                }
                let outcome = perform(plan, graph, &task);
                if leaf {
                    watch.leaves.fetch_sub(1, Ordering::AcqRel);
                }
                watch.now.fetch_sub(1, Ordering::AcqRel);
                watch.ran.fetch_add(1, Ordering::Relaxed);
                // The arguments drop here, outside the lock, and their buffer goes back.
                let args = match task.work {
                    Work::Compute { mut args, .. } => {
                        args.clear();
                        Some(args)
                    }
                    _ => None,
                };
                guard = lock(shared);
                if let Some(scheduler) = guard.as_mut() {
                    scheduler.complete(plan, graph, grant, task.id, outcome, &mut report);
                    if let Some(args) = args {
                        scheduler.recycle(args);
                    }
                }
                shared.1.notify_all();
                drop(guard);
                // Released values drop here, outside the lock too.
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

/// Runs the nested fan-out of `items` on eight threads under `grant`, and checks its outputs
/// against `Plan::run`'s.
fn eight_threads(items: i64, grant: &dyn Grant) -> Watch {
    let graph = nested(items);
    let plan = graph
        .prepare(&Request {
            outputs: vec!["outer_sum".to_owned()],
            inputs: BTreeSet::new(),
            overrides: BTreeSet::new(),
        })
        .unwrap_or_else(|error| unreachable!("the request plans: {error}"));
    let want = plan
        .run_by_id(&graph, &mut Sequential, |_, _| None, &mut [])
        .unwrap_or_else(|error| unreachable!("the reference run succeeds: {error}"));

    let seeded: Vec<Option<Slot<Val>>> = vec![None; graph.nodes().len()];
    let scheduler = Scheduler::new(
        &plan,
        &graph,
        Arc::new(Layout::new(&plan, &graph)),
        seeded,
        Options::default(),
        &mut Report::default(),
    );
    let shared: Shared = (Mutex::new(Some(scheduler)), Condvar::new());
    let deadline = Instant::now() + Duration::from_secs(120);
    let watch = Watch::default();
    thread::scope(|scope| {
        for _ in 0..8 {
            scope.spawn(|| work(&plan, &graph, grant, &shared, deadline, &watch));
        }
    });
    let scheduler = lock(&shared)
        .take()
        .unwrap_or_else(|| unreachable!("the scheduler is still there"));
    let got = scheduler
        .finish(&plan, &graph)
        .unwrap_or_else(|error| unreachable!("the run succeeds: {error}"));
    assert_eq!(got, want);
    watch
}

#[test]
fn eight_threads_behind_a_mutex_give_plan_runs_outputs() {
    let watch = eight_threads(1_000, &Unlimited);
    // Every outer branch probed, pulled, iterated and finished its inner block.
    assert!(watch.ran.load(Ordering::Relaxed) > 5_000);
    // The threads overlapped.
    assert!(watch.most.load(Ordering::Acquire) > 1);
}

#[test]
fn eight_threads_never_exceed_their_limits() {
    let graph = nested(1);
    let leaf = graph
        .id_of("leaf")
        .unwrap_or_else(|| unreachable!("the graph has a leaf"));
    let mut caps = vec![None; graph.nodes().len()];
    caps[leaf.index()] = NonZeroUsize::new(2);
    let limits = Limits::new(NonZeroUsize::new(3), caps, Vec::new(), Vec::new());
    let watch = eight_threads(300, &limits);
    assert!(watch.most.load(Ordering::Acquire) <= 3);
    assert!(watch.most_leaves.load(Ordering::Acquire) <= 2);
    assert_eq!(limits.held(Resource::Worker), 0);
}
