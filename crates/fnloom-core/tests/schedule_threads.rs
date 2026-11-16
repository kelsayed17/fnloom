//! The scheduler driven from eight `std` threads behind one lock, against `Plan::run`.

use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use fnloom_core::graph::Graph;
use fnloom_core::limits::Unlimited;
use fnloom_core::node::{Collect, Dependency, InputSpec, Node, NodeId, NodeSpec};
use fnloom_core::plan::{Plan, Request};
use fnloom_core::run::{Arg, Blocks, Evaluate, Item, Slot};
use fnloom_core::schedule::{ArgValue, Next, Options, Outcome, Scheduler, Seat, Task, Work, probe};

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

/// Runs one task, after a short sleep that depends on its id.
fn perform(plan: &Plan, graph: &Graph<Op>, task: &Task<Val>) -> Outcome<Val, String, String> {
    let micros = u64::from(task.id.0.wrapping_mul(2_654_435_761) % 50);
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

/// One worker: takes tasks until the run is done, waiting at most `deadline` in all.
fn work(plan: &Plan, graph: &Graph<Op>, shared: &Shared, deadline: Instant, ran: &AtomicUsize) {
    let mut guard = lock(shared);
    loop {
        assert!(Instant::now() < deadline, "the run hung");
        let Some(scheduler) = guard.as_mut() else {
            return;
        };
        match scheduler.next(plan, graph, &Unlimited, Seat::Take) {
            Next::Task(task) => {
                drop(guard);
                let outcome = perform(plan, graph, &task);
                ran.fetch_add(1, Ordering::Relaxed);
                guard = lock(shared);
                if let Some(scheduler) = guard.as_mut() {
                    let report = scheduler.complete(plan, graph, &Unlimited, task.id, outcome);
                    drop(report);
                }
                shared.1.notify_all();
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

#[test]
fn eight_threads_behind_a_mutex_give_plan_runs_outputs() {
    let graph = nested(1_000);
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
    let (scheduler, _) = Scheduler::new(&plan, &graph, seeded, Options::default());
    let shared: Shared = (Mutex::new(Some(scheduler)), Condvar::new());
    let deadline = Instant::now() + Duration::from_secs(120);
    let ran = AtomicUsize::new(0);
    thread::scope(|scope| {
        for _ in 0..8 {
            scope.spawn(|| work(&plan, &graph, &shared, deadline, &ran));
        }
    });
    let scheduler = lock(&shared)
        .take()
        .unwrap_or_else(|| unreachable!("the scheduler is still there"));
    let got = scheduler
        .finish(&plan, &graph)
        .unwrap_or_else(|error| unreachable!("the run succeeds: {error}"));
    assert_eq!(got, want);
    // Every outer branch probed, pulled, iterated and finished its inner block.
    assert!(ran.load(Ordering::Relaxed) > 5_000);
}
