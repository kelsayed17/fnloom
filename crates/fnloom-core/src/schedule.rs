//! The scheduler: one run of a plan as a state machine that hands out tasks.
//!
//! `Plan::run` runs a plan from start to end on the caller's thread. A `Scheduler` runs the
//! same plan by handing out tasks: `next` gives the ready task with the lowest order key, and
//! `complete` takes back its outcome and makes what waited for it ready. It has no threads,
//! no clock and no locks, and borrows nothing: every method takes the plan and graph it was
//! made with. So a caller with several workers keeps it behind one lock, and a caller that
//! steps it one task at a time reproduces `Plan::run` task for task.
//!
//! Each scope of the plan, the outer steps or one branch of a block, is an instance of a
//! template computed once per run: per step, the steps of its scope it waits for and the
//! readers of its value there. A value is released once its last reader in its scope has
//! taken its arguments, and handed back in `Report::released`, never dropped here.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap};

use crate::branch::{Need, declined_wrongly, needs, node_error};
use crate::graph::Graph;
use crate::limits::Grant;
use crate::node::{Collect, Dependency, Edge, NodeId, as_u32};
use crate::plan::{Action, BlockId, Plan, Step};
use crate::run::{Item, Outputs, RunError, Slot, broken, planned_to_compute};

/// Names a task `Scheduler::next` handed out, for `Scheduler::complete`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct TaskId(pub u32);

/// Where a task falls in reference order (`Plan::run`'s).
///
/// The (step position, branch index) pairs of the expands whose blocks hold the task's step,
/// from the outermost plan inwards, then the step's position in its own scope. Keys compare
/// lexicographically, a prefix first: the pull of item `i` of the expand keyed `k` is `k`
/// then `i`, just before that branch's first step, `k`, `i`, `0`.
#[derive(Clone, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct OrderKey(pub Vec<u32>);

/// One argument of a `Compute` task, by the absence rules.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ArgValue<V> {
    /// The value, absent or not: an absent one only reaches an `Admits` edge.
    One(V),
    /// A collect's values, one per branch in branch order, absent ones dropped by
    /// `Collect::Present`; `Blocks::gather` makes the argument from them.
    Gather(Vec<V>),
    /// An `Optional` edge from an absent value: the argument is left out, as for an optional
    /// input that wasn't supplied.
    Omitted,
}

/// What a task does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Work<V> {
    /// Runs the node's payload on `args`, sorted by input id.
    ///
    /// An expand (`expands`) iterates the value it computed on the same worker, and completes
    /// with `Outcome::Iterator`, or with `Outcome::Value` when the value is absent. A `late`
    /// step, one outside every block that `Plan::is_late` names, is looked up first: a hit
    /// completes it with `Outcome::Hit` and no call.
    Compute {
        /// The arguments, by input node, sorted by id.
        args: Vec<(NodeId, ArgValue<V>)>,
        /// Whether the node is an expand.
        expands: bool,
        /// Whether to ask `Evaluate::lookup` first.
        late: bool,
    },
    /// Iterates an expand's override or hit (`Blocks::iterate`); completes with
    /// `Outcome::Iterator`.
    Iterate {
        /// The iterable.
        iterable: V,
    },
    /// Pulls the next item (`Blocks::next_item`); completes with `Outcome::Item`.
    Pull {
        /// The iterator, as `Iterator` gave it.
        iterator: V,
    },
    /// Ends the iterator (`Blocks::finish_items`), exhausted or stopped; completes with
    /// `Outcome::Finished`.
    Finish {
        /// The iterator.
        iterator: V,
        /// `true` once its items are exhausted; `false` when the run stopped first.
        exhausted: bool,
    },
    /// Asks a new branch's cache hits, with `probe`; completes with `Outcome::Needs`.
    Probe {
        /// The block whose branch it is.
        block: BlockId,
    },
}

/// A unit of work handed out by `Scheduler::next`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Task<V> {
    /// Its name, for `Scheduler::complete`.
    pub id: TaskId,
    /// The node it computes; for an iterate, pull, finish or probe, the expand.
    pub node: NodeId,
    /// The branch it runs in, one item index per level of nesting, outermost first. For a
    /// pull or a finish, the expand's own branch; for a probe, the new branch.
    pub branch: Vec<usize>,
    /// Its order key.
    pub key: OrderKey,
    /// What it does.
    pub work: Work<V>,
}

/// What a task gave, for `Scheduler::complete`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome<V, E, K> {
    /// A compute's value, and whether it is absent: only a node that declines
    /// (`Node::declines`) gives an absent one.
    Value {
        /// The value.
        value: V,
        /// Whether it is absent.
        absent: bool,
    },
    /// A late compute's value from `Evaluate::lookup`, and whether it is absent
    /// (`Node::may_be_absent` and `Evaluate::is_absent`).
    Hit {
        /// The value.
        value: V,
        /// Whether it is absent.
        absent: bool,
    },
    /// An expand's iterator, from a compute or an iterate.
    Iterator(V),
    /// A pull's item.
    Item(Item<V>),
    /// What finishing the iterator returned.
    Finished(V),
    /// A probe's answer: the steps of the branch that are hits, by position in the block's
    /// steps, with their values and whether each is absent; and each step's single-flight
    /// key, by position, or `None`.
    Needs {
        /// `(position, value, absent)` for each hit.
        hits: Vec<(usize, V, bool)>,
        /// The key of each step by position, or `None`.
        keys: Vec<Option<K>>,
    },
    /// The task failed: the node's own failure, for a compute, or the expand's, for an
    /// iterate, a pull or a finish.
    Failed(E),
    /// A probe's lookup of this node's step failed: that node's own failure.
    LookupFailed(NodeId, E),
}

/// What `Scheduler::next` gives.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Next<V> {
    /// A task to run, then to complete.
    Task(Task<V>),
    /// Nothing to hand out now: tasks are in flight, or the grant refused the ready ones.
    Wait,
    /// Nothing is left: call `Scheduler::finish`.
    Done,
}

/// What a `complete` (or `Scheduler::new`) settled, for the caller to act on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Report<V> {
    /// Values no step reads any more: the caller drops them, so a value is never dropped
    /// inside the scheduler, which a lock may guard.
    pub released: Vec<V>,
    /// Nodes skipped because a `Required` argument is absent, with their branch and order
    /// key, in key order: the events `Observer::skipped` reports.
    pub skipped: Vec<(NodeId, Vec<usize>, OrderKey)>,
    /// Expands whose items were exhausted and whose branches all ended, with their branch and
    /// item count: the events `Observer::post_expand` reports.
    pub expanded: Vec<(NodeId, Vec<usize>, usize)>,
    /// Steps that completed as hits, with their branch and value, in plan order within a
    /// branch: the events `Observer::hit_in` reports.
    pub hits: Vec<(NodeId, Vec<usize>, V)>,
}

impl<V> Default for Report<V> {
    fn default() -> Self {
        Self {
            released: Vec::new(),
            skipped: Vec::new(),
            expanded: Vec::new(),
            hits: Vec::new(),
        }
    }
}

/// Who asks `Scheduler::next` for a task, which decides the slots the task takes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Seat {
    /// A worker, which takes a worker slot.
    Take,
    /// The caller of a run inside a run, which already holds a worker slot.
    Held,
    /// The run's one lent worker seat (`Scheduler::lend`).
    Lent,
    /// A task that holds no thread, such as an async node.
    Threadless,
}

/// How a scheduler runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Options {
    /// An expand's iterator is pulled only while fewer than this many of its branches are
    /// unfinished; at least 1.
    pub in_flight: usize,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            in_flight: usize::MAX,
        }
    }
}

/// Runs a `Probe` task: asks `lookup` for the steps of one branch of `block`, in the reverse
/// need order `Plan::run` asks them, and gives the outcome to complete the probe with.
///
/// `lookup(node)` gives the step's hit, if any, with whether it is absent, and its
/// single-flight key, if any. A hit's own arguments are not needed through it, so the steps
/// only it needed are not asked. `wanted` is scratch, reused across calls. After a failing
/// lookup nothing more is asked, and the outcome is `Outcome::LookupFailed`.
pub fn probe<P, V, E, K>(
    plan: &Plan,
    graph: &Graph<P>,
    block: BlockId,
    wanted: &mut Vec<bool>,
    mut lookup: impl FnMut(NodeId) -> Result<(Option<(V, bool)>, Option<K>), E>,
) -> Outcome<V, E, K> {
    wanted.resize(graph.nodes().len(), false);
    let count = plan
        .blocks()
        .get(block.index())
        .map_or(0, |sub| sub.steps.len());
    let mut keys: Vec<Option<K>> = (0..count).map(|_| None).collect();
    let mut hits = Vec::new();
    let mut failure = None;
    let mut need = Vec::new();
    needs(plan, graph, block, wanted, &mut need, |position, step| {
        if failure.is_some() {
            return false;
        }
        match lookup(step.node) {
            Ok((hit, key)) => {
                keys[position] = key;
                hit.map(|(value, absent)| hits.push((position, value, absent)))
                    .is_some()
            }
            Err(error) => {
                failure = Some((step.node, error));
                false
            }
        }
    });
    match failure {
        Some((node, error)) => Outcome::LookupFailed(node, error),
        None => Outcome::Needs { hits, keys },
    }
}

/// A value of a scope that its readers count down: a step's, the branch's item, or what a
/// collect reads of a nested block.
#[derive(Clone, Copy)]
enum Use {
    Slot(u32),
    Item,
    Gathered(u32),
}

/// A readers count that is never counted down: an output's, or a collected node's.
const KEEP: u32 = u32::MAX;

/// What one scope's instances start from: the outer plan's, or a block's.
#[derive(Default)]
struct Template {
    /// Per step, how many steps of the scope it waits for.
    waiting: Vec<u32>,
    /// Per step, how many steps of the scope read its value (`KEEP`: never released).
    readers: Vec<u32>,
    /// Per step, the steps that wait for it: `after[spans.0..spans.1]`.
    after: Vec<u32>,
    after_spans: Vec<(u32, u32)>,
    /// Per step, what its arguments read of the scope: taken when it is handed out.
    uses: Vec<Use>,
    use_spans: Vec<(u32, u32)>,
    /// Per expand step, what its block reads of the scope: kept until its block has run.
    block_uses: Vec<Use>,
    block_spans: Vec<(u32, u32)>,
    /// How many steps read the branch's item.
    item_readers: u32,
    /// Per entry of the gathered table, how many steps read it.
    gather_readers: Vec<u32>,
}

/// Every scope's template, and where each collected node's gathered values sit.
#[derive(Default)]
struct Layout {
    /// The outer plan's, then each block's, by `BlockId` plus one.
    templates: Vec<Template>,
    /// Per collected node, its entry in the gathered table of its expand's scope.
    gather_at: Vec<u32>,
}

/// The scope holding `node`'s step: 0 for the outer plan, a block's id plus one.
fn scope_of(plan: &Plan, node: NodeId) -> usize {
    plan.within(node).map_or(0, |block| block.index() + 1)
}

/// The steps of scope `scope`.
fn steps_of(plan: &Plan, scope: usize) -> &[Step] {
    match scope.checked_sub(1) {
        None => plan.steps(),
        Some(block) => plan
            .blocks()
            .get(block)
            .map_or(&[], |sub| sub.steps.as_slice()),
    }
}

/// The edge of `node` that its step's argument `at`, `arg`, is read by.
fn edge_of<P>(plan: &Plan, graph: &Graph<P>, node: NodeId, at: usize, arg: NodeId) -> Option<Edge> {
    let inputs = &graph.node(node).inputs;
    match plan.arg_edges(node).get(at) {
        Some(&edge) => inputs.get(edge as usize).copied(),
        // A plan without an expand keeps no edge table; this is asked only of an absent value.
        None => inputs.iter().find(|edge| edge.node == arg).copied(),
    }
}

/// What reading `arg` takes of scope `scope`, if its value lives there.
fn use_of<P>(
    plan: &Plan,
    graph: &Graph<P>,
    scope: usize,
    arg: NodeId,
    collects: bool,
    gather_at: &[u32],
) -> Option<Use> {
    let node = graph.node(arg);
    if collects {
        let expand = node.block?;
        return (scope_of(plan, expand) == scope).then(|| Use::Gathered(gather_at[arg.index()]));
    }
    if node.is_expand() {
        let block = plan.block_of(arg)?;
        return (block.index() + 1 == scope).then_some(Use::Item);
    }
    (scope_of(plan, arg) == scope).then(|| Use::Slot(as_u32(plan.position(arg))))
}

impl Layout {
    fn new<P>(plan: &Plan, graph: &Graph<P>) -> Self {
        let count = graph.nodes().len();
        let mut collected = vec![false; count];
        for sub in plan.blocks() {
            for &node in &sub.collected {
                collected[node.index()] = true;
            }
        }
        let mut gather_at = vec![u32::MAX; count];
        let templates = (0..=plan.blocks().len())
            .map(|scope| template(plan, graph, scope, &collected, &mut gather_at))
            .collect();
        Self {
            templates,
            gather_at,
        }
    }
}

/// The template of scope `scope`.
fn template<P>(
    plan: &Plan,
    graph: &Graph<P>,
    scope: usize,
    collected: &[bool],
    gather_at: &mut [u32],
) -> Template {
    let steps = steps_of(plan, scope);
    let depth = match scope.checked_sub(1) {
        None => 0,
        Some(block) => graph.block_path(plan.blocks()[block].expand).len() + 1,
    };
    let mut gathered = 0;
    for step in steps {
        if let Some(block) = plan.block_of(step.node) {
            for &node in &plan.blocks()[block.index()].collected {
                gather_at[node.index()] = gathered;
                gathered += 1;
            }
        }
    }
    let mut t = Template {
        waiting: vec![0; steps.len()],
        readers: vec![0; steps.len()],
        gather_readers: vec![0; gathered as usize],
        ..Template::default()
    };
    // (step waited for, step waiting), for the `after` table.
    let mut edges: Vec<(u32, u32)> = Vec::new();
    let count = |t: &mut Template, found: Option<Use>| match found {
        Some(Use::Slot(at)) => t.readers[at as usize] += 1,
        Some(Use::Item) => t.item_readers += 1,
        Some(Use::Gathered(at)) => t.gather_readers[at as usize] += 1,
        None => {}
    };
    for (position, step) in steps.iter().enumerate() {
        let node = graph.node(step.node);
        let mut wait = |place: NodeId| {
            if let Some(unit) = graph.unit_in(place, depth) {
                if unit != step.node && scope_of(plan, unit) == scope {
                    edges.push((as_u32(plan.position(unit)), as_u32(position)));
                }
            }
        };
        if step.action == Action::Compute {
            for edge in &node.inputs {
                // A collect reads its source's block as a whole, once that has run.
                let place = match (edge.collect, graph.node(edge.node).block) {
                    (Some(_), Some(expand)) => expand,
                    _ => edge.node,
                };
                wait(place);
            }
        }
        let block = plan.block_of(step.node);
        if let Some(block) = block {
            for &read in &plan.blocks()[block.index()].reads {
                wait(graph.read_place(read, step.node));
            }
        }
        let start = as_u32(t.uses.len());
        if step.action == Action::Compute {
            for (at, &arg) in step.args.iter().enumerate() {
                let collects = plan
                    .arg_edges(step.node)
                    .get(at)
                    .and_then(|&edge| node.inputs.get(edge as usize))
                    .is_some_and(|edge| edge.collect.is_some());
                let found = use_of(plan, graph, scope, arg, collects, gather_at);
                count(&mut t, found);
                t.uses.extend(found);
            }
        }
        t.use_spans.push((start, as_u32(t.uses.len())));
        let start = as_u32(t.block_uses.len());
        if let Some(block) = block {
            for &read in &plan.blocks()[block.index()].reads {
                let place = graph.read_place(read, step.node);
                let found = if place == read {
                    use_of(plan, graph, scope, read, false, gather_at)
                } else {
                    // The gathered values of a sibling block, which live where its expand is.
                    (scope_of(plan, place) == scope).then(|| Use::Gathered(gather_at[read.index()]))
                };
                count(&mut t, found);
                t.block_uses.extend(found);
            }
        }
        t.block_spans.push((start, as_u32(t.block_uses.len())));
    }
    for (position, step) in steps.iter().enumerate() {
        let output = scope == 0 && plan.outputs().binary_search(&step.node).is_ok();
        if output || collected[step.node.index()] {
            t.readers[position] = KEEP;
        }
    }
    t.fill_after(&edges);
    t
}

impl Template {
    /// Fills `waiting` and the `after` table from `(step waited for, step waiting)` pairs, by
    /// counting sort on the step waited for.
    fn fill_after(&mut self, edges: &[(u32, u32)]) {
        let mut starts = vec![0_u32; self.waiting.len() + 1];
        for &(before, _) in edges {
            starts[before as usize + 1] += 1;
        }
        for at in 1..starts.len() {
            starts[at] += starts[at - 1];
        }
        self.after = vec![0; edges.len()];
        let mut fill = starts.clone();
        for &(before, waiting) in edges {
            self.waiting[waiting as usize] += 1;
            self.after[fill[before as usize] as usize] = waiting;
            fill[before as usize] += 1;
        }
        self.after_spans = starts.windows(2).map(|pair| (pair[0], pair[1])).collect();
    }
}

/// What a collect reads of one node: its value in each branch, or the absent value of an
/// expand that had none.
enum Collected<V> {
    Branches(Vec<Slot<V>>),
    Absent(V),
}

/// One step of one scope instance.
struct StepState<V> {
    slot: Option<Slot<V>>,
    waiting: u32,
    readers: u32,
    need: Need,
    progress: Progress,
    /// Its arguments' readers were counted down.
    used: bool,
    /// Its block's reads' readers were counted down.
    block_used: bool,
}

/// How far a step of an instance got.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Progress {
    Waiting,
    /// Made ready, once.
    Queued,
    /// Its value is in (or it was skipped), and its dependents were told.
    Done,
}

/// One instance of a scope: the outer plan, or one branch of a block.
struct Instance<V, K> {
    /// Per step, its single-flight key from the branch's probe; empty for the outer plan.
    keys: Vec<Option<K>>,
    template: usize,
    /// The frame this branch belongs to and its index there; `None` for the outer plan.
    up: Option<(usize, usize)>,
    /// The instance holding the frame's expand step.
    parent: Option<usize>,
    /// The order key of the branch, which each step's key extends.
    prefix: Vec<u32>,
    branch: Vec<usize>,
    item: Option<V>,
    item_readers: u32,
    steps: Vec<StepState<V>>,
    /// Per entry of the template's gathered table, what its collects read, and its readers.
    gathered: Vec<(Option<Collected<V>>, u32)>,
    /// Steps not yet done.
    remaining: usize,
}

/// One expand's iteration: its iterator, its branches so far, and what they collected.
struct Frame<V> {
    node: NodeId,
    /// `None` for an expand requested alone, whose items are counted and dropped.
    block: Option<BlockId>,
    /// The instance holding the expand step, and the step's position.
    scope: usize,
    position: usize,
    /// The expand step's order key.
    key: Vec<u32>,
    iterator: Option<V>,
    /// The pull queued or running.
    pull: Option<usize>,
    /// A pull is held back by `Options::in_flight`.
    pull_wanted: bool,
    pulled: usize,
    /// Branches pulled and not yet ended.
    open: usize,
    /// The finish queued or running.
    finishing: Option<usize>,
    finished: Option<V>,
    declined: Option<V>,
    /// Per node of `SubPlan::collected`, its slot from each branch, by branch index.
    collected: Vec<Vec<Option<Slot<V>>>>,
}

/// What an entry stands for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Compute,
    /// A compute whose `Required` argument is absent: settled without a task.
    Skip,
    Iterate,
    Pull,
    Finish(bool),
    Probe,
}

/// A task ready or in flight.
struct Entry {
    kind: Kind,
    key: OrderKey,
    node: NodeId,
    /// The instance of a compute, skip, iterate or probe; the frame of a pull or finish.
    at: usize,
    /// The step of a compute, skip or iterate.
    position: usize,
    /// The seat it was handed out to, while it runs.
    seat: Option<Seat>,
}

impl Entry {
    /// The node whose slots the grant gives the task: a compute's or a pull's.
    const fn granted(&self) -> Option<NodeId> {
        match self.kind {
            Kind::Compute | Kind::Pull => Some(self.node),
            _ => None,
        }
    }
}

/// What settling a completion goes through, kept on a list so nesting never recurses.
enum Event {
    /// A step of an instance is done.
    Resolved(usize, usize),
    /// Every step of a branch is done.
    Ended(usize),
    /// A frame may be complete.
    Frame(usize),
}

/// Values by index, reusing the free ones.
struct Slab<T> {
    items: Vec<Option<T>>,
    free: Vec<usize>,
}

impl<T> Default for Slab<T> {
    fn default() -> Self {
        Self {
            items: Vec::new(),
            free: Vec::new(),
        }
    }
}

impl<T> Slab<T> {
    fn insert(&mut self, item: T) -> usize {
        if let Some(at) = self.free.pop() {
            self.items[at] = Some(item);
            at
        } else {
            self.items.push(Some(item));
            self.items.len() - 1
        }
    }

    fn remove(&mut self, at: usize) -> Option<T> {
        let item = self.items.get_mut(at).and_then(Option::take);
        if item.is_some() {
            self.free.push(at);
        }
        item
    }

    fn get(&self, at: usize) -> Option<&T> {
        self.items.get(at).and_then(Option::as_ref)
    }

    fn get_mut(&mut self, at: usize) -> Option<&mut T> {
        self.items.get_mut(at).and_then(Option::as_mut)
    }
}

/// A read of an argument's value.
enum Read<'a, V> {
    Value(&'a Slot<V>),
    Item(&'a V),
    Gathered(&'a Collected<V>),
}

/// `prefix` extended by `tail`.
fn extend(prefix: &[u32], tail: &[u32]) -> Vec<u32> {
    let mut key = Vec::with_capacity(prefix.len() + tail.len());
    key.extend_from_slice(prefix);
    key.extend_from_slice(tail);
    key
}

/// Why `plan` and `graph` can't be run together: an id the graph doesn't have.
fn mismatch<P>(plan: &Plan, graph: &Graph<P>) -> Option<String> {
    let count = graph.nodes().len();
    let steps = plan
        .steps()
        .iter()
        .chain(plan.blocks().iter().flat_map(|sub| &sub.steps));
    let mut ids = steps.flat_map(|step| std::iter::once(&step.node).chain(&step.args));
    let blocks = plan.blocks().iter().flat_map(|sub| {
        std::iter::once(&sub.expand)
            .chain(&sub.collected)
            .chain(&sub.reads)
    });
    ids.by_ref()
        .chain(blocks)
        .chain(plan.outputs())
        .find(|id| id.index() >= count)
        .map(|id| {
            format!(
                "the plan reads node {}, which the graph does not have",
                id.0
            )
        })
}

/// The scheduler of one run of one plan.
///
/// Made by `new` with the plan, its graph and the seeded inputs and overrides; stepped with
/// `next` and `complete`, which take that same plan and graph; ended by `finish`. A plan or
/// graph other than the ones it was made with is a `RunError::BrokenPlan`.
pub struct Scheduler<V, E, K> {
    /// The plan's and graph's sizes, checked on every call.
    shape: (usize, usize, usize, usize),
    layout: Layout,
    options: Options,
    instances: Slab<Instance<V, K>>,
    frames: Slab<Frame<V>>,
    entries: Slab<Entry>,
    ready: BinaryHeap<Reverse<(OrderKey, usize)>>,
    running: usize,
    todo: Vec<Event>,
    report: Report<V>,
    failures: Vec<(OrderKey, RunError<E>)>,
    /// What finishing a stopped run's iterators raised: dropped by `finish`.
    dropped: Vec<E>,
    stopping: bool,
    /// Frames to finish as stopped, sorted by key: handed out last first, innermost first.
    closing: Vec<(OrderKey, usize)>,
    wanted: Vec<bool>,
    need: Vec<Need>,
    /// The running compute of each single-flight key, and the steps waiting for it.
    flights: BTreeMap<K, Flight>,
    /// The lent worker seat: `None` until `lend`, then whether a task holds it.
    lent: Option<bool>,
    /// The node of the lowest-key ready task the grant refused at the last `next`.
    refused: Option<NodeId>,
}

/// The running compute of a single-flight key, and the entries waiting for its value.
struct Flight {
    task: usize,
    waiters: Vec<usize>,
}

/// The outer plan's instance.
const OUTER: usize = 0;

impl<V: Clone, E, K: Ord + Clone> Scheduler<V, E, K> {
    /// A scheduler for one run of `plan`, prepared from `graph`, and what its seeded values
    /// settle before any task: consumers skipped by an absent override.
    ///
    /// `seeded` holds, by node id, the value of each `UseInput` and `UseOverride` step,
    /// absent only for an override of a node that may be absent (`Node::may_be_absent`)
    /// whose value `Evaluate::is_absent` says is. A missing one fails the run with
    /// `RunError::MissingValues`, and any other is released.
    pub fn new<P>(
        plan: &Plan,
        graph: &Graph<P>,
        seeded: Vec<Option<Slot<V>>>,
        options: Options,
    ) -> (Self, Report<V>) {
        let mut this = Self {
            shape: Self::shape_of(plan, graph),
            layout: Layout::default(),
            options,
            instances: Slab::default(),
            frames: Slab::default(),
            entries: Slab::default(),
            ready: BinaryHeap::new(),
            running: 0,
            todo: Vec::new(),
            report: Report::default(),
            failures: Vec::new(),
            dropped: Vec::new(),
            stopping: false,
            closing: Vec::new(),
            wanted: Vec::new(),
            need: Vec::new(),
            flights: BTreeMap::new(),
            lent: None,
            refused: None,
        };
        let detail = mismatch(plan, graph).or_else(|| {
            (seeded.len() != graph.nodes().len()).then(|| {
                format!(
                    "{} seeded values for a graph of {} nodes",
                    seeded.len(),
                    graph.nodes().len()
                )
            })
        });
        if let Some(detail) = detail {
            this.release_all(seeded.into_iter().flatten().map(|slot| slot.value));
            this.fail(OrderKey::default(), broken(detail));
            return this.out();
        }
        this.layout = Layout::new(plan, graph);
        this.wanted = vec![false; graph.nodes().len()];
        this.open_instance(0, None, None, Vec::new(), Vec::new(), None);
        let mut seeded = seeded;
        let mut missing = Vec::new();
        for (position, step) in plan.steps().iter().enumerate() {
            if !matches!(step.action, Action::UseInput | Action::UseOverride) {
                continue;
            }
            match seeded.get_mut(step.node.index()).and_then(Option::take) {
                Some(slot) => this.state(OUTER, position).slot = Some(slot),
                None => missing.push(graph.node(step.node).name.clone()),
            }
        }
        this.release_all(seeded.into_iter().flatten().map(|slot| slot.value));
        if !missing.is_empty() {
            missing.sort();
            this.fail(OrderKey::default(), RunError::MissingValues(missing));
            return this.out();
        }
        for (position, step) in plan.steps().iter().enumerate() {
            match step.action {
                Action::UseOverride if graph.node(step.node).is_expand() => {
                    // Iterated once what its block reads is in; an absent one has no block.
                    let state = this.state(OUTER, position);
                    if let Some(slot) = state.slot.take_if(|slot| slot.absent) {
                        this.absent_expand(plan, OUTER, position, slot.value);
                    }
                }
                Action::UseInput | Action::UseOverride | Action::SkipOptionalInput => {
                    this.todo.push(Event::Resolved(OUTER, position));
                }
                Action::Compute => {}
            }
        }
        this.settle(plan, graph);
        for (position, step) in plan.steps().iter().enumerate() {
            let iterates = step.action == Action::UseOverride && graph.node(step.node).is_expand();
            let pending = step.action == Action::Compute || iterates;
            if pending && this.state(OUTER, position).waiting == 0 {
                this.make_ready(plan, graph, OUTER, position);
            }
        }
        this.drain(plan, graph);
        this.out()
    }

    /// The ready task with the lowest order key that `grant` lets `seat` start, skipping
    /// over the ones it refuses.
    ///
    /// `Next::Wait` when tasks are in flight or the grant refused every ready one, and
    /// `Next::Done` when nothing is left. Once the run is stopping, only `Finish` tasks are
    /// handed out, one per open iterator with no pull in flight, innermost first.
    pub fn next<P>(
        &mut self,
        plan: &Plan,
        graph: &Graph<P>,
        grant: &dyn Grant,
        seat: Seat,
    ) -> Next<V> {
        if self.shape != Self::shape_of(plan, graph) {
            self.fail(OrderKey::default(), another_plan());
        }
        self.refused = None;
        if seat == Seat::Lent && self.lent != Some(false) {
            // No seat is lent, or a task holds it.
            return if self.stopping {
                self.idle_closing()
            } else {
                self.idle()
            };
        }
        loop {
            if self.stopping {
                let next = self.next_closing(grant, seat);
                if seat == Seat::Lent && matches!(next, Next::Task(_)) {
                    self.lent = Some(true);
                }
                return next;
            }
            let mut aside = Vec::new();
            let mut found = None;
            let mut first = true;
            while let Some(Reverse((key, id))) = self.ready.pop() {
                let Some(entry) = self.entries.get(id) else {
                    continue;
                };
                // A skip is settled by `complete`, which reports it, once it is the lowest.
                if entry.kind == Kind::Skip {
                    aside.push(Reverse((key, id)));
                    continue;
                }
                // A step whose key a running compute has waits for its value.
                let flight = self.flight_key(graph, entry);
                if let Some(running) = flight.as_ref().and_then(|key| self.flights.get_mut(key)) {
                    running.waiters.push(id);
                    continue;
                }
                if !grant.try_start(entry.granted(), seat) {
                    if first {
                        self.refused = Some(entry.node);
                    }
                    first = false;
                    aside.push(Reverse((key, id)));
                    continue;
                }
                found = Some((id, flight));
                break;
            }
            self.ready.extend(aside);
            let Some((id, flight)) = found else {
                return self.idle();
            };
            match self.task(plan, graph, id, seat) {
                Ok(task) => {
                    self.running += 1;
                    if let Some(key) = flight {
                        self.flights.insert(
                            key,
                            Flight {
                                task: id,
                                waiters: Vec::new(),
                            },
                        );
                    }
                    if seat == Seat::Lent {
                        self.lent = Some(true);
                    }
                    return Next::Task(task);
                }
                Err(failure) => {
                    if let Some(entry) = self.entries.remove(id) {
                        grant.release(entry.granted(), seat);
                        self.fail(entry.key, failure);
                    }
                }
            }
        }
    }

    /// Takes back the outcome of task `id`, gives its slots back to `grant`, and makes what
    /// waited for it ready.
    ///
    /// A `Failed` outcome stops the run (`stop`) and is kept with the task's order key; the
    /// lowest key's is what `finish` returns. An outcome after the run stopped is dropped,
    /// its values released. The report holds what this settled.
    pub fn complete<P>(
        &mut self,
        plan: &Plan,
        graph: &Graph<P>,
        grant: &dyn Grant,
        id: TaskId,
        outcome: Outcome<V, E, K>,
    ) -> Report<V> {
        let running = self
            .entries
            .get(id.0 as usize)
            .is_some_and(|entry| entry.seat.is_some());
        let entry = if running {
            self.entries.remove(id.0 as usize)
        } else {
            None
        };
        let Some(entry) = entry else {
            self.discard(outcome);
            self.fail(
                OrderKey::default(),
                broken(format!("task {} completed but not running", id.0)),
            );
            return std::mem::take(&mut self.report);
        };
        let seat = entry.seat.unwrap_or(Seat::Take);
        grant.release(entry.granted(), seat);
        self.running -= 1;
        if seat == Seat::Lent && self.lent.is_some() {
            self.lent = Some(false);
        }
        if self.shape == Self::shape_of(plan, graph) {
            let waiters = match self.flight_key(graph, &entry) {
                Some(key)
                    if self
                        .flights
                        .get(&key)
                        .is_some_and(|flight| flight.task == id.0 as usize) =>
                {
                    self.flights
                        .remove(&key)
                        .map(|flight| flight.waiters)
                        .unwrap_or_default()
                }
                _ => Vec::new(),
            };
            // A value or a declared absence completes every waiter as a hit.
            let shared = match &outcome {
                Outcome::Value { value, absent } if !waiters.is_empty() && !self.stopping => {
                    Some((value.clone(), *absent))
                }
                _ => None,
            };
            self.land(plan, graph, entry, outcome);
            for waiter in waiters {
                match &shared {
                    Some((value, absent)) => {
                        self.share(plan, graph, waiter, value.clone(), *absent);
                    }
                    // A failed original: the waiter runs itself.
                    None => {
                        if let Some(entry) = self.entries.get(waiter) {
                            self.ready.push(Reverse((entry.key.clone(), waiter)));
                        }
                    }
                }
            }
            self.settle(plan, graph);
            self.drain(plan, graph);
        } else {
            self.discard(outcome);
            self.fail(OrderKey::default(), another_plan());
        }
        std::mem::take(&mut self.report)
    }

    /// Stops the run: no compute, pull, iterate or probe is handed out any more, and each open
    /// iterator is finished as stopped. Tasks in flight still complete.
    pub fn stop(&mut self) {
        if self.stopping {
            return;
        }
        self.stopping = true;
        let mut closing = Vec::new();
        for (at, frame) in self.frames.items.iter().enumerate() {
            let Some(frame) = frame else {
                continue;
            };
            let busy = |entry: Option<usize>| {
                entry
                    .and_then(|entry| self.entries.get(entry))
                    .is_some_and(|entry| entry.seat.is_some())
            };
            if frame.iterator.is_some() && !busy(frame.pull) && !busy(frame.finishing) {
                closing.push(at);
            }
        }
        for frame in closing {
            self.close_later(frame);
        }
    }

    /// Whether the run is stopping: it stopped or failed.
    #[must_use]
    pub const fn is_stopping(&self) -> bool {
        self.stopping
    }

    /// The node of the lowest-key ready task, when the grant refused it at the last `next`
    /// and nothing of this run is in flight: what the run waits for a slot of.
    #[must_use]
    pub fn blocked(&self) -> Option<NodeId> {
        self.refused.filter(|_| self.running == 0)
    }

    /// Gives the run one lent worker seat, which `next` hands out to with `Seat::Lent`: to
    /// one task at a time, and free again once that task completes.
    pub const fn lend(&mut self) {
        if self.lent.is_none() {
            self.lent = Some(false);
        }
    }

    /// The run's outputs, by node id in `Plan::outputs` order, or its failure.
    ///
    /// The failure with the lowest order key, when there is one. A run stopped without one
    /// returns its outputs if they were all computed, and a `RunError::BrokenPlan` naming the
    /// first missing one otherwise.
    ///
    /// # Errors
    ///
    /// The run's first failure in key order, or a `RunError::BrokenPlan`.
    pub fn finish<P>(mut self, plan: &Plan, graph: &Graph<P>) -> Result<Outputs<V>, RunError<E>> {
        if self.shape != Self::shape_of(plan, graph) {
            return Err(another_plan());
        }
        if !self.failures.is_empty() {
            let mut failures = std::mem::take(&mut self.failures);
            failures.sort_by(|a, b| a.0.cmp(&b.0));
            if let Some((_, failure)) = failures.into_iter().next() {
                return Err(failure);
            }
        }
        plan.outputs()
            .iter()
            .map(|&id| {
                let slot = self
                    .instances
                    .get_mut(OUTER)
                    .and_then(|outer| outer.steps.get_mut(plan.position(id)))
                    .and_then(|state| state.slot.take());
                match slot {
                    Some(slot) => Ok((id, slot.value)),
                    None => Err(broken(format!(
                        "output '{}' has no value at the end",
                        graph.node(id).name
                    ))),
                }
            })
            .collect()
    }

    fn shape_of<P>(plan: &Plan, graph: &Graph<P>) -> (usize, usize, usize, usize) {
        (
            plan.steps().len(),
            plan.blocks().len(),
            plan.outputs().len(),
            graph.nodes().len(),
        )
    }

    fn out(mut self) -> (Self, Report<V>) {
        let report = std::mem::take(&mut self.report);
        (self, report)
    }

    fn release(&mut self, value: V) {
        self.report.released.push(value);
    }

    fn release_all(&mut self, values: impl IntoIterator<Item = V>) {
        self.report.released.extend(values);
    }

    fn release_collected(&mut self, collected: Collected<V>) {
        match collected {
            Collected::Branches(slots) => self.release_all(slots.into_iter().map(|s| s.value)),
            Collected::Absent(value) => self.release(value),
        }
    }

    /// Releases every value an unused outcome holds; a closing error is kept for `finish`.
    fn discard(&mut self, outcome: Outcome<V, E, K>) {
        match outcome {
            Outcome::Value { value, .. }
            | Outcome::Hit { value, .. }
            | Outcome::Iterator(value)
            | Outcome::Finished(value)
            | Outcome::Item(Item::Next(value) | Item::Declined(value)) => self.release(value),
            Outcome::Item(Item::Done) => {}
            Outcome::Needs { hits, .. } => {
                self.release_all(hits.into_iter().map(|(_, value, _)| value));
            }
            Outcome::Failed(error) | Outcome::LookupFailed(_, error) => self.dropped.push(error),
        }
    }

    /// Keeps `failure` with its key and stops the run.
    fn fail(&mut self, key: OrderKey, failure: RunError<E>) {
        self.failures.push((key, failure));
        self.stop();
    }

    fn state(&mut self, instance: usize, position: usize) -> &mut StepState<V> {
        &mut self.instances.items[instance]
            .as_mut()
            .unwrap_or_else(|| unreachable!("instance {instance} is open"))
            .steps[position]
    }

    fn instance(&self, instance: usize) -> &Instance<V, K> {
        self.instances
            .get(instance)
            .unwrap_or_else(|| unreachable!("instance {instance} is open"))
    }

    fn instance_mut(&mut self, instance: usize) -> &mut Instance<V, K> {
        self.instances
            .get_mut(instance)
            .unwrap_or_else(|| unreachable!("instance {instance} is open"))
    }

    fn frame_mut(&mut self, frame: usize) -> &mut Frame<V> {
        self.frames
            .get_mut(frame)
            .unwrap_or_else(|| unreachable!("frame {frame} is open"))
    }

    /// The single-flight key of a compute entry, from its branch's probe; never an expand's,
    /// whose iterator can't be shared.
    fn flight_key<P>(&self, graph: &Graph<P>, entry: &Entry) -> Option<K> {
        if entry.kind != Kind::Compute || graph.node(entry.node).is_expand() {
            return None;
        }
        self.instances
            .get(entry.at)?
            .keys
            .get(entry.position)
            .cloned()
            .flatten()
    }

    /// Completes the waiting entry `waiter` with the value its key's compute gave.
    fn share<P>(&mut self, plan: &Plan, graph: &Graph<P>, waiter: usize, value: V, absent: bool) {
        let Some(entry) = self.entries.remove(waiter) else {
            return;
        };
        let branch = self.instance(entry.at).branch.clone();
        self.report.hits.push((entry.node, branch, value.clone()));
        self.use_args(entry.at, entry.position);
        self.computed(plan, graph, entry.at, entry.position, value, absent);
    }

    /// `Next::Done` once a stopped run has nothing in flight, else `Next::Wait`.
    fn idle_closing(&self) -> Next<V> {
        if self.running == 0 && self.closing.is_empty() {
            Next::Done
        } else {
            Next::Wait
        }
    }

    /// `Next::Done` when nothing is left, else `Next::Wait`.
    fn idle(&self) -> Next<V> {
        if self.running == 0 && self.ready.is_empty() {
            Next::Done
        } else {
            Next::Wait
        }
    }

    /// The next finish of a stopped run's iterators, innermost first.
    fn next_closing(&mut self, grant: &dyn Grant, seat: Seat) -> Next<V> {
        while let Some(&(_, at)) = self.closing.last() {
            let iterator = self
                .frames
                .get_mut(at)
                .and_then(|frame| frame.iterator.take());
            let Some(iterator) = iterator else {
                self.closing.pop();
                continue;
            };
            if !grant.try_start(None, seat) {
                self.frame_mut(at).iterator = Some(iterator);
                return Next::Wait;
            }
            self.closing.pop();
            let frame = self.frame_mut(at);
            let (node, scope) = (frame.node, frame.scope);
            let key = OrderKey(extend(&frame.key, &[as_u32(frame.pulled)]));
            let branch = self.instance(scope).branch.clone();
            let id = self.entries.insert(Entry {
                kind: Kind::Finish(false),
                key: key.clone(),
                node,
                at,
                position: 0,
                seat: Some(seat),
            });
            self.frame_mut(at).finishing = Some(id);
            self.running += 1;
            return Next::Task(Task {
                id: TaskId(as_u32(id)),
                node,
                branch,
                key,
                work: Work::Finish {
                    iterator,
                    exhausted: false,
                },
            });
        }
        self.idle_closing()
    }

    /// Queues frame `at` to be finished as stopped.
    fn close_later(&mut self, at: usize) {
        let Some(frame) = self.frames.get(at) else {
            return;
        };
        // By the expand's own key: an inner frame's extends its outer one's.
        let key = OrderKey(frame.key.clone());
        if self.closing.iter().any(|&(_, frame)| frame == at) {
            return;
        }
        let place = self.closing.partition_point(|(other, _)| *other < key);
        self.closing.insert(place, (key, at));
    }

    /// Builds the task of entry `id` for `seat`, taking what it needs.
    fn task<P>(
        &mut self,
        plan: &Plan,
        graph: &Graph<P>,
        id: usize,
        seat: Seat,
    ) -> Result<Task<V>, RunError<E>> {
        let Some(entry) = self.entries.get(id) else {
            return Err(broken(format!("task {id} is not ready")));
        };
        let (kind, node, at, position, key) = (
            entry.kind,
            entry.node,
            entry.at,
            entry.position,
            entry.key.clone(),
        );
        let (branch, work) = match kind {
            Kind::Compute => {
                let instance = self.instance(at);
                let step = &steps_of(plan, instance.template)[position];
                let target = graph.node(node);
                if target.payload().is_none() {
                    return Err(planned_to_compute(target));
                }
                let args = self.arguments(plan, graph, at, step)?;
                let instance = self.instance(at);
                let late = plan.is_late(node) && instance.up.is_none();
                let branch = instance.branch.clone();
                self.use_args(at, position);
                (
                    branch,
                    Work::Compute {
                        args,
                        expands: target.is_expand(),
                        late,
                    },
                )
            }
            Kind::Iterate => {
                let Some(slot) = self.state(at, position).slot.take() else {
                    return Err(broken(format!(
                        "expand '{}' has no value",
                        graph.node(node).name
                    )));
                };
                (
                    self.instance(at).branch.clone(),
                    Work::Iterate {
                        iterable: slot.value,
                    },
                )
            }
            Kind::Pull | Kind::Finish(_) => {
                let frame = self.frame_mut(at);
                let iterator = if kind == Kind::Pull {
                    frame.iterator.clone()
                } else {
                    frame.iterator.take()
                };
                let scope = frame.scope;
                let Some(iterator) = iterator else {
                    return Err(broken(format!(
                        "expand '{}' has no iterator",
                        graph.node(node).name
                    )));
                };
                let work = match kind {
                    Kind::Finish(exhausted) => Work::Finish {
                        iterator,
                        exhausted,
                    },
                    _ => Work::Pull { iterator },
                };
                (self.instance(scope).branch.clone(), work)
            }
            Kind::Probe => {
                let instance = self.instance(at);
                let block = BlockId(as_u32(instance.template.saturating_sub(1)));
                (instance.branch.clone(), Work::Probe { block })
            }
            Kind::Skip => return Err(broken(format!("task {id} is a skip"))),
        };
        if let Some(entry) = self.entries.get_mut(id) {
            entry.seat = Some(seat);
        }
        Ok(Task {
            id: TaskId(as_u32(id)),
            node,
            branch,
            key,
            work,
        })
    }

    /// The instance on `instance`'s path, itself or an outer one, of scope `scope`.
    fn holder(&self, mut instance: usize, scope: usize) -> Option<usize> {
        loop {
            let found = self.instances.get(instance)?;
            if found.template == scope {
                return Some(instance);
            }
            instance = found.parent?;
        }
    }

    /// The value `arg` has for a step of `instance`, read by a collect or not.
    fn read<P>(
        &self,
        plan: &Plan,
        graph: &Graph<P>,
        instance: usize,
        arg: NodeId,
        collects: bool,
    ) -> Option<Read<'_, V>> {
        let node = graph.node(arg);
        if collects {
            let holder = self.holder(instance, scope_of(plan, node.block?))?;
            let at = *self.layout.gather_at.get(arg.index())? as usize;
            let gathered = self.instances.get(holder)?.gathered.get(at)?;
            return gathered.0.as_ref().map(Read::Gathered);
        }
        if node.is_expand() {
            let holder = self.holder(instance, plan.block_of(arg)?.index() + 1)?;
            return self.instances.get(holder)?.item.as_ref().map(Read::Item);
        }
        let holder = self.holder(instance, scope_of(plan, arg))?;
        let state = self.instances.get(holder)?.steps.get(plan.position(arg))?;
        state.slot.as_ref().map(Read::Value)
    }

    /// The absent value that skips `step` of `instance`: its first `Required` argument's,
    /// in argument order.
    fn skip_value<P>(
        &self,
        plan: &Plan,
        graph: &Graph<P>,
        instance: usize,
        step: &Step,
    ) -> Result<Option<V>, RunError<E>> {
        for (at, &arg) in step.args.iter().enumerate() {
            let collects = plan.has_expands()
                && edge_of(plan, graph, step.node, at, arg).is_some_and(|e| e.collect.is_some());
            let absent = match self.read(plan, graph, instance, arg, collects) {
                Some(Read::Value(slot)) => slot.absent.then_some(&slot.value),
                Some(Read::Gathered(Collected::Absent(value))) => Some(value),
                Some(Read::Item(_) | Read::Gathered(Collected::Branches(_))) => None,
                None => return Err(no_value(graph, step.node, arg)),
            };
            if let Some(value) = absent {
                let dependency = edge_of(plan, graph, step.node, at, arg)
                    .map_or(Dependency::Required, |edge| edge.dependency);
                if dependency == Dependency::Required {
                    return Ok(Some(value.clone()));
                }
            }
        }
        Ok(None)
    }

    /// The arguments of `step` of `instance`, by the absence rules.
    fn arguments<P>(
        &self,
        plan: &Plan,
        graph: &Graph<P>,
        instance: usize,
        step: &Step,
    ) -> Result<Vec<(NodeId, ArgValue<V>)>, RunError<E>> {
        if plan.has_expands() && plan.arg_edges(step.node).len() != step.args.len() {
            return Err(broken(format!(
                "node '{}' has no edge it was planned to read",
                graph.node(step.node).name
            )));
        }
        let mut args = Vec::with_capacity(step.args.len());
        for (at, &arg) in step.args.iter().enumerate() {
            let edge = plan
                .has_expands()
                .then(|| edge_of(plan, graph, step.node, at, arg))
                .flatten();
            let collect = edge.and_then(|edge| edge.collect);
            let absent = |value: &V| {
                let dependency = edge
                    .or_else(|| edge_of(plan, graph, step.node, at, arg))
                    .map_or(Dependency::Required, |edge| edge.dependency);
                match dependency {
                    Dependency::Admits { .. } => Ok(ArgValue::One(value.clone())),
                    Dependency::Optional => Ok(ArgValue::Omitted),
                    Dependency::Required => Err(no_value(graph, step.node, arg)),
                }
            };
            let value = match self.read(plan, graph, instance, arg, collect.is_some()) {
                Some(Read::Value(slot)) if slot.absent => absent(&slot.value)?,
                Some(Read::Value(slot)) => ArgValue::One(slot.value.clone()),
                Some(Read::Item(value)) => ArgValue::One(value.clone()),
                Some(Read::Gathered(Collected::Absent(value))) => absent(value)?,
                Some(Read::Gathered(Collected::Branches(slots))) => ArgValue::Gather(
                    slots
                        .iter()
                        .filter(|slot| collect == Some(Collect::All) || !slot.absent)
                        .map(|slot| slot.value.clone())
                        .collect(),
                ),
                None => return Err(no_value(graph, step.node, arg)),
            };
            args.push((arg, value));
        }
        Ok(args)
    }

    /// Opens an instance of scope `template`.
    fn open_instance(
        &mut self,
        template: usize,
        up: Option<(usize, usize)>,
        parent: Option<usize>,
        prefix: Vec<u32>,
        branch: Vec<usize>,
        item: Option<V>,
    ) -> usize {
        let t = &self.layout.templates[template];
        let steps: Vec<StepState<V>> = t
            .waiting
            .iter()
            .zip(&t.readers)
            .map(|(&waiting, &readers)| StepState {
                slot: None,
                waiting,
                readers,
                need: Need::Run,
                progress: Progress::Waiting,
                used: false,
                block_used: false,
            })
            .collect();
        let item_readers = t.item_readers;
        let gathered = t.gather_readers.iter().map(|&r| (None, r)).collect();
        let remaining = steps.len();
        let instance = Instance {
            keys: Vec::new(),
            template,
            up,
            parent,
            prefix,
            branch,
            item: None,
            item_readers,
            steps,
            gathered,
            remaining,
        };
        let at = self.instances.insert(instance);
        if let Some(item) = item {
            if item_readers == 0 {
                self.release(item);
            } else {
                self.instance_mut(at).item = Some(item);
            }
        }
        at
    }

    /// Queues a ready entry.
    fn queue(
        &mut self,
        kind: Kind,
        key: Vec<u32>,
        node: NodeId,
        at: usize,
        position: usize,
    ) -> usize {
        let key = OrderKey(key);
        let id = self.entries.insert(Entry {
            kind,
            key: key.clone(),
            node,
            at,
            position,
            seat: None,
        });
        self.ready.push(Reverse((key, id)));
        id
    }

    /// Makes step `position` of `instance` ready: a compute, or a skip.
    fn make_ready<P>(&mut self, plan: &Plan, graph: &Graph<P>, instance: usize, position: usize) {
        let state = self.state(instance, position);
        if state.progress != Progress::Waiting {
            return;
        }
        state.progress = Progress::Queued;
        let iterates = state.need == Need::Hit;
        let found = self.instance(instance);
        let step = &steps_of(plan, found.template)[position];
        let key = extend(&found.prefix, &[as_u32(position)]);
        // An expand's override or hit, whose iterable is in its slot, once what its block
        // reads is in.
        if iterates || step.action == Action::UseOverride {
            self.queue(Kind::Iterate, key, step.node, instance, position);
            return;
        }
        let kind = match self.skip_value(plan, graph, instance, step) {
            Ok(Some(_)) => Kind::Skip,
            Ok(None) => Kind::Compute,
            Err(failure) => {
                self.fail(OrderKey(key), failure);
                return;
            }
        };
        self.queue(kind, key, step.node, instance, position);
    }

    /// Queues the iterate of an expand's override or hit, keeping the iterable in its slot.
    fn queue_iterate(&mut self, instance: usize, position: usize, node: NodeId, iterable: V) {
        self.state(instance, position).slot = Some(Slot {
            value: iterable,
            absent: false,
        });
        let key = extend(&self.instance(instance).prefix, &[as_u32(position)]);
        self.queue(Kind::Iterate, key, node, instance, position);
    }

    /// Counts down the readers of `uses`, releasing each value whose last reader that was.
    fn count_down(&mut self, instance: usize, uses: (u32, u32), block: bool) {
        let template = self.instance(instance).template;
        for at in uses.0..uses.1 {
            let t = &self.layout.templates[template];
            let found = if block {
                t.block_uses[at as usize]
            } else {
                t.uses[at as usize]
            };
            let place = self.instance_mut(instance);
            let freed = match found {
                Use::Slot(at) => {
                    let state = &mut place.steps[at as usize];
                    if state.readers == KEEP || state.readers == 0 {
                        continue;
                    }
                    state.readers -= 1;
                    if state.readers == 0 && state.progress == Progress::Done {
                        state.slot.take().map(|slot| slot.value)
                    } else {
                        None
                    }
                }
                Use::Item => {
                    place.item_readers = place.item_readers.saturating_sub(1);
                    if place.item_readers == 0 {
                        place.item.take()
                    } else {
                        None
                    }
                }
                Use::Gathered(at) => {
                    let entry = &mut place.gathered[at as usize];
                    entry.1 = entry.1.saturating_sub(1);
                    if entry.1 == 0 {
                        if let Some(collected) = entry.0.take() {
                            self.release_collected(collected);
                        }
                    }
                    None
                }
            };
            if let Some(value) = freed {
                self.release(value);
            }
        }
    }

    /// Counts down what step `position`'s arguments read, once.
    fn use_args(&mut self, instance: usize, position: usize) {
        let state = self.state(instance, position);
        if state.used {
            return;
        }
        state.used = true;
        let template = self.instance(instance).template;
        let span = self.layout.templates[template].use_spans[position];
        self.count_down(instance, span, false);
    }

    /// Counts down what the block of expand step `position` reads, once.
    fn use_block(&mut self, instance: usize, position: usize) {
        let state = self.state(instance, position);
        if state.block_used {
            return;
        }
        state.block_used = true;
        let template = self.instance(instance).template;
        let span = self.layout.templates[template].block_spans[position];
        self.count_down(instance, span, true);
    }

    /// Settles the events queued, until none is left.
    fn settle<P>(&mut self, plan: &Plan, graph: &Graph<P>) {
        while let Some(event) = self.todo.pop() {
            match event {
                Event::Resolved(instance, position) => {
                    self.resolved(plan, graph, instance, position);
                }
                Event::Ended(instance) => self.end_branch(plan, graph, instance),
                Event::Frame(frame) => self.check_frame(plan, graph, frame),
            }
        }
    }

    /// Settles the skips that are the lowest ready entries, reporting each.
    fn drain<P>(&mut self, plan: &Plan, graph: &Graph<P>) {
        while !self.stopping {
            let Some(&Reverse((_, id))) = self.ready.peek() else {
                return;
            };
            match self.entries.get(id) {
                Some(entry) if entry.kind == Kind::Skip => {}
                Some(_) => return,
                None => {
                    self.ready.pop();
                    continue;
                }
            }
            self.ready.pop();
            let Some(entry) = self.entries.remove(id) else {
                continue;
            };
            self.skip(plan, graph, &entry);
            self.settle(plan, graph);
        }
    }

    /// Settles a skip: the node is absent, holding the absent value that skipped it.
    fn skip<P>(&mut self, plan: &Plan, graph: &Graph<P>, entry: &Entry) {
        let (instance, position) = (entry.at, entry.position);
        let template = self.instance(instance).template;
        let step = &steps_of(plan, template)[position];
        let value = match self.skip_value(plan, graph, instance, step) {
            Ok(Some(value)) => value,
            Ok(None) => {
                // Not absent after all: compute it.
                self.queue(
                    Kind::Compute,
                    entry.key.0.clone(),
                    entry.node,
                    instance,
                    position,
                );
                return;
            }
            Err(failure) => {
                self.fail(entry.key.clone(), failure);
                return;
            }
        };
        self.use_args(instance, position);
        let branch = self.instance(instance).branch.clone();
        self.report
            .skipped
            .push((entry.node, branch, entry.key.clone()));
        if graph.node(entry.node).is_expand() {
            self.absent_expand(plan, instance, position, value);
        } else {
            self.state(instance, position).slot = Some(Slot {
                value,
                absent: true,
            });
            self.todo.push(Event::Resolved(instance, position));
        }
    }

    /// An expand step whose value is absent: its block has no branches, and each of its
    /// collects reads the absent value.
    fn absent_expand(&mut self, plan: &Plan, instance: usize, position: usize, value: V) {
        let template = self.instance(instance).template;
        let node = steps_of(plan, template)[position].node;
        if let Some(block) = plan.block_of(node) {
            for &collected in &plan.blocks()[block.index()].collected {
                let at = self.layout.gather_at[collected.index()] as usize;
                let entry = &mut self.instance_mut(instance).gathered[at];
                if entry.1 == 0 {
                    self.release(value.clone());
                } else {
                    entry.0 = Some(Collected::Absent(value.clone()));
                }
            }
        }
        self.state(instance, position).slot = Some(Slot {
            value,
            absent: true,
        });
        self.todo.push(Event::Resolved(instance, position));
    }

    /// Step `position` of `instance` is done: its dependents are told.
    fn resolved<P>(&mut self, plan: &Plan, graph: &Graph<P>, instance: usize, position: usize) {
        let state = self.state(instance, position);
        if state.progress == Progress::Done {
            return;
        }
        state.progress = Progress::Done;
        let freed = if state.readers == 0 {
            state.slot.take().map(|slot| slot.value)
        } else {
            None
        };
        if let Some(value) = freed {
            self.release(value);
        }
        let template = self.instance(instance).template;
        let node = steps_of(plan, template)[position].node;
        if plan.block_of(node).is_some() {
            self.use_block(instance, position);
        }
        let (start, end) = self.layout.templates[template].after_spans[position];
        for at in start..end {
            let waiting = self.layout.templates[template].after[at as usize] as usize;
            let state = self.state(instance, waiting);
            state.waiting = state.waiting.saturating_sub(1);
            if state.waiting == 0 && state.need == Need::Run && state.progress == Progress::Waiting
            {
                self.make_ready(plan, graph, instance, waiting);
            }
        }
        let found = self.instance_mut(instance);
        found.remaining = found.remaining.saturating_sub(1);
        if found.remaining == 0 && found.up.is_some() {
            self.todo.push(Event::Ended(instance));
        }
    }

    /// Every step of branch `instance` is done: what its collects read moves to its frame.
    fn end_branch<P>(&mut self, plan: &Plan, graph: &Graph<P>, instance: usize) {
        let Some(mut found) = self.instances.remove(instance) else {
            return;
        };
        let Some((frame, index)) = found.up else {
            return;
        };
        let block = found.template - 1;
        let mut missing = None;
        for (k, &node) in plan.blocks()[block].collected.iter().enumerate() {
            let slot = found
                .steps
                .get_mut(plan.position(node))
                .and_then(|state| state.slot.take());
            match slot {
                Some(slot) => self.frame_mut(frame).collected[k][index] = Some(slot),
                None => missing = missing.or(Some(node)),
            }
        }
        self.release_all(found.item.take());
        let leftover: Vec<V> = found
            .steps
            .into_iter()
            .filter_map(|state| state.slot.map(|slot| slot.value))
            .collect();
        self.release_all(leftover);
        for (collected, _) in found.gathered {
            if let Some(collected) = collected {
                self.release_collected(collected);
            }
        }
        if let Some(node) = missing {
            self.fail(
                OrderKey(found.prefix),
                broken(format!(
                    "collected node '{}' has no value at the end of a branch",
                    graph.node(node).name
                )),
            );
            return;
        }
        let in_flight = self.options.in_flight.max(1);
        let stopping = self.stopping;
        let at = self.frame_mut(frame);
        at.open -= 1;
        if at.pull_wanted && at.open < in_flight && !stopping {
            self.queue_pull(frame);
        }
        self.todo.push(Event::Frame(frame));
    }

    /// Completes frame `at` once its items are over and its branches have all ended.
    fn check_frame<P>(&mut self, plan: &Plan, graph: &Graph<P>, at: usize) {
        let Some(frame) = self.frames.get(at) else {
            return;
        };
        if frame.open > 0 || (frame.declined.is_none() && frame.finished.is_none()) {
            return;
        }
        let Some(frame) = self.frames.remove(at) else {
            return;
        };
        let (instance, position) = (frame.scope, frame.position);
        if let Some(value) = frame.declined {
            for slots in frame.collected {
                self.release_all(slots.into_iter().flatten().map(|slot| slot.value));
            }
            self.absent_expand(plan, instance, position, value);
            return;
        }
        let Some(value) = frame.finished else {
            return;
        };
        if let Some(block) = frame.block {
            let collected = &plan.blocks()[block.index()].collected;
            for (&node, slots) in collected.iter().zip(frame.collected) {
                let Some(slots) = slots.into_iter().collect::<Option<Vec<_>>>() else {
                    self.fail(
                        OrderKey(frame.key.clone()),
                        broken(format!(
                            "a branch of '{}' was lost",
                            graph.node(frame.node).name
                        )),
                    );
                    return;
                };
                let entry_at = self.layout.gather_at[node.index()] as usize;
                let entry = &mut self.instance_mut(instance).gathered[entry_at];
                if entry.1 == 0 {
                    self.release_collected(Collected::Branches(slots));
                } else {
                    entry.0 = Some(Collected::Branches(slots));
                }
            }
        }
        let branch = self.instance(instance).branch.clone();
        self.report
            .expanded
            .push((frame.node, branch, frame.pulled));
        self.state(instance, position).slot = Some(Slot {
            value,
            absent: false,
        });
        self.todo.push(Event::Resolved(instance, position));
    }

    /// Queues frame `at`'s next pull.
    fn queue_pull(&mut self, at: usize) {
        let frame = self.frame_mut(at);
        frame.pull_wanted = false;
        let key = extend(&frame.key, &[as_u32(frame.pulled)]);
        let node = frame.node;
        let id = self.queue(Kind::Pull, key, node, at, 0);
        self.frame_mut(at).pull = Some(id);
    }

    /// Opens the frame of expand step `position` of `instance`, which gave `iterator`.
    fn open_frame(&mut self, plan: &Plan, instance: usize, position: usize, iterator: V) {
        let found = self.instance(instance);
        let node = steps_of(plan, found.template)[position].node;
        let block = plan.block_of(node);
        let key = extend(&found.prefix, &[as_u32(position)]);
        let collected = block.map_or(0, |block| plan.blocks()[block.index()].collected.len());
        let at = self.frames.insert(Frame {
            node,
            block,
            scope: instance,
            position,
            key,
            iterator: Some(iterator),
            pull: None,
            pull_wanted: false,
            pulled: 0,
            open: 0,
            finishing: None,
            finished: None,
            declined: None,
            collected: (0..collected).map(|_| Vec::new()).collect(),
        });
        if self.stopping {
            self.close_later(at);
        } else {
            self.queue_pull(at);
        }
    }

    /// Takes in what a task gave.
    fn land<P>(&mut self, plan: &Plan, graph: &Graph<P>, entry: Entry, outcome: Outcome<V, E, K>) {
        match entry.kind {
            Kind::Pull => self.frame_mut(entry.at).pull = None,
            Kind::Finish(_) => self.frame_mut(entry.at).finishing = None,
            _ => {}
        }
        // A failure counts even once the run stopped, so the lowest key's is raised; what a
        // finish closing an iterator raises doesn't.
        let failed = matches!(outcome, Outcome::Failed(_) | Outcome::LookupFailed(..))
            && entry.kind != Kind::Finish(false);
        if self.stopping && !failed {
            self.discard_stopped(plan, &entry, outcome);
            return;
        }
        let (kind, at) = (entry.kind, entry.at);
        self.take_in(plan, graph, entry, outcome);
        // A pull whose iterator the stop could not finish while it ran.
        if self.stopping
            && kind == Kind::Pull
            && self
                .frames
                .get(at)
                .is_some_and(|frame| frame.iterator.is_some())
        {
            self.close_later(at);
        }
    }

    /// Takes in what a task gave, while the run goes on, or a failure.
    fn take_in<P>(
        &mut self,
        plan: &Plan,
        graph: &Graph<P>,
        entry: Entry,
        outcome: Outcome<V, E, K>,
    ) {
        match (entry.kind, outcome) {
            (Kind::Compute, Outcome::Value { value, absent }) => {
                self.computed(plan, graph, entry.at, entry.position, value, absent);
            }
            (Kind::Compute, Outcome::Hit { value, absent }) => {
                let branch = self.instance(entry.at).branch.clone();
                self.report.hits.push((entry.node, branch, value.clone()));
                self.computed(plan, graph, entry.at, entry.position, value, absent);
            }
            (Kind::Compute | Kind::Iterate, Outcome::Iterator(iterator)) => {
                self.open_frame(plan, entry.at, entry.position, iterator);
            }
            (Kind::Pull, Outcome::Item(item)) => self.pulled(graph, entry.at, item),
            (Kind::Finish(true), Outcome::Finished(value)) => {
                self.frame_mut(entry.at).finished = Some(value);
                self.todo.push(Event::Frame(entry.at));
            }
            (Kind::Probe, Outcome::Needs { hits, keys }) => {
                self.instance_mut(entry.at).keys = keys;
                self.probed(plan, graph, entry.at, hits);
            }
            (Kind::Probe, Outcome::LookupFailed(node, error)) => {
                let found = self.instance(entry.at);
                let key = extend(&found.prefix, &[as_u32(plan.position(node))]);
                let failure = node_error(graph, graph.node(node), error, &found.branch, Vec::new());
                self.fail(OrderKey(key), failure);
            }
            (kind, Outcome::Failed(error)) => {
                let branch = match kind {
                    Kind::Pull | Kind::Finish(_) => {
                        let scope = self.frame_mut(entry.at).scope;
                        self.instance(scope).branch.clone()
                    }
                    _ => self.instance(entry.at).branch.clone(),
                };
                let failure = node_error(graph, graph.node(entry.node), error, &branch, Vec::new());
                self.fail(entry.key, failure);
            }
            (_, outcome) => {
                self.discard(outcome);
                self.fail(
                    entry.key,
                    broken(format!(
                        "a task of node '{}' gave an outcome it cannot give",
                        graph.node(entry.node).name
                    )),
                );
            }
        }
    }

    /// Drops what a task of a stopping run gave, finishing any iterator it leaves open.
    fn discard_stopped(&mut self, plan: &Plan, entry: &Entry, outcome: Outcome<V, E, K>) {
        match outcome {
            // An iterate's or an expand's iterator opens a frame only to be finished.
            Outcome::Iterator(iterator) => {
                self.open_frame(plan, entry.at, entry.position, iterator);
            }
            other => self.discard(other),
        }
        if entry.kind == Kind::Pull
            && self
                .frames
                .get(entry.at)
                .is_some_and(|frame| frame.iterator.is_some())
        {
            self.close_later(entry.at);
        }
    }

    /// A compute's value is in.
    fn computed<P>(
        &mut self,
        plan: &Plan,
        graph: &Graph<P>,
        instance: usize,
        position: usize,
        value: V,
        absent: bool,
    ) {
        let node = steps_of(plan, self.instance(instance).template)[position].node;
        if graph.node(node).is_expand() {
            if absent {
                self.absent_expand(plan, instance, position, value);
            } else {
                self.queue_iterate(instance, position, node, value);
            }
            return;
        }
        self.state(instance, position).slot = Some(Slot { value, absent });
        self.todo.push(Event::Resolved(instance, position));
    }

    /// A pull's item is in.
    fn pulled<P>(&mut self, graph: &Graph<P>, at: usize, item: Item<V>) {
        match item {
            Item::Next(value) => {
                let in_flight = self.options.in_flight.max(1);
                let frame = self.frame_mut(at);
                let index = frame.pulled;
                frame.pulled += 1;
                let Some(block) = frame.block else {
                    self.release(value);
                    self.queue_pull(at);
                    return;
                };
                frame.open += 1;
                for slots in &mut frame.collected {
                    slots.push(None);
                }
                let (scope, node, open) = (frame.scope, frame.node, frame.open);
                let prefix = extend(&frame.key, &[as_u32(index)]);
                let mut branch = self.instance(scope).branch.clone();
                branch.push(index);
                let instance = self.open_instance(
                    block.index() + 1,
                    Some((at, index)),
                    Some(scope),
                    prefix.clone(),
                    branch,
                    Some(value),
                );
                self.queue(Kind::Probe, prefix, node, instance, 0);
                if open < in_flight {
                    self.queue_pull(at);
                } else {
                    self.frame_mut(at).pull_wanted = true;
                }
            }
            Item::Done => {
                let frame = self.frame_mut(at);
                let key = extend(&frame.key, &[as_u32(frame.pulled)]);
                let node = frame.node;
                let id = self.queue(Kind::Finish(true), key, node, at, 0);
                self.frame_mut(at).finishing = Some(id);
            }
            Item::Declined(value) => {
                let frame = self.frame_mut(at);
                let node = graph.node(frame.node);
                if node.declines {
                    let iterator = frame.iterator.take();
                    frame.declined = Some(value);
                    self.release_all(iterator);
                    self.todo.push(Event::Frame(at));
                } else {
                    // `Plan::run` closes the iterator of a block it entered, and not that of
                    // an expand requested alone.
                    let iterator = if frame.block.is_none() {
                        frame.iterator.take()
                    } else {
                        None
                    };
                    let key = OrderKey(frame.key.clone());
                    self.release(value);
                    self.release_all(iterator);
                    self.fail(key, declined_wrongly(node));
                }
            }
        }
    }

    /// A probe's hits are in: the branch's needs are decided, its hits are in their slots,
    /// and its first steps are ready.
    fn probed<P>(
        &mut self,
        plan: &Plan,
        graph: &Graph<P>,
        instance: usize,
        hits: Vec<(usize, V, bool)>,
    ) {
        let template = self.instance(instance).template;
        let steps = steps_of(plan, template);
        let mut table: Vec<Option<(V, bool)>> = (0..steps.len()).map(|_| None).collect();
        for (position, value, absent) in hits {
            match table.get_mut(position) {
                Some(place) => *place = Some((value, absent)),
                None => self.release(value),
            }
        }
        let block = BlockId(as_u32(template - 1));
        let mut need = std::mem::take(&mut self.need);
        needs(
            plan,
            graph,
            block,
            &mut self.wanted,
            &mut need,
            |position, _| table[position].is_some(),
        );
        let branch = self.instance(instance).branch.clone();
        for (position, step) in steps.iter().enumerate() {
            let found = need.get(position).copied().unwrap_or(Need::Skip);
            self.state(instance, position).need = found;
            match found {
                Need::Hit => {
                    let Some((value, absent)) = table[position].take() else {
                        continue;
                    };
                    self.report
                        .hits
                        .push((step.node, branch.clone(), value.clone()));
                    self.use_args(instance, position);
                    if graph.node(step.node).is_expand() && !absent {
                        // Iterated once what its block reads is in.
                        self.state(instance, position).slot = Some(Slot {
                            value,
                            absent: false,
                        });
                        if self.state(instance, position).waiting == 0 {
                            self.make_ready(plan, graph, instance, position);
                        }
                    } else {
                        self.computed(plan, graph, instance, position, value, absent);
                    }
                }
                Need::Skip => {
                    self.use_args(instance, position);
                    self.todo.push(Event::Resolved(instance, position));
                }
                Need::Run => {
                    if self.state(instance, position).waiting == 0 {
                        self.make_ready(plan, graph, instance, position);
                    }
                }
            }
        }
        self.need = need;
        self.release_all(table.into_iter().flatten().map(|(value, _)| value));
        if self.instance(instance).remaining == 0 {
            self.todo.push(Event::Ended(instance));
        }
    }
}

/// The `RunError::BrokenPlan` for a call with another plan or graph than the scheduler's.
fn another_plan<E>() -> RunError<E> {
    broken("the scheduler was given another plan or graph than it was made with".to_owned())
}

/// The `RunError::BrokenPlan` for an argument with no value.
fn no_value<P, E>(graph: &Graph<P>, consumer: NodeId, arg: NodeId) -> RunError<E> {
    broken(format!(
        "argument '{}' of node '{}' has no value",
        graph.node(arg).name,
        graph.node(consumer).name
    ))
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use std::cell::Cell;
    use std::collections::BTreeMap;
    use std::rc::Rc;
    use std::sync::atomic::{AtomicBool, Ordering};

    use super::*;
    use crate::limits::{Limits, Slot as LimitSlot, Unlimited};
    use crate::node::{Dependency, NodeSpec};
    use crate::run::{Arg, Blocks, Evaluate};
    use crate::testing::blocks::{
        End, Fan, Log, Pay, Val, absent_expand, chain, declines, declining_details, echo, expand,
        fan, first, hit, items, nested, node, num, nums, sibling_collect, tens,
    };
    use crate::testing::{collect, plain, request};

    use Val::{Absent, List, Num};

    type Sched = Scheduler<Val, String, String>;
    type Outputs = Result<BTreeMap<String, Val>, RunError<String>>;

    /// The seeded values of `plan`: `inputs` and `overrides` by name, an override absent as
    /// `Plan::run` asks it.
    fn seed(
        graph: &Graph<Pay>,
        plan: &Plan,
        inputs: &[(&str, Val)],
        overrides: &[(&str, Val)],
        eval: &Fan,
    ) -> Vec<Option<Slot<Val>>> {
        let mut seeded = vec![None; graph.nodes().len()];
        for step in plan.steps() {
            let node = graph.node(step.node);
            let from = match step.action {
                Action::UseInput => inputs,
                Action::UseOverride => overrides,
                _ => continue,
            };
            if let Some((_, value)) = from.iter().find(|(name, _)| *name == node.name) {
                let absent = step.action == Action::UseOverride
                    && node.may_be_absent
                    && eval.is_absent(value);
                seeded[step.node.index()] = Some(Slot {
                    value: value.clone(),
                    absent,
                });
            }
        }
        seeded
    }

    /// Runs `task` as a binding would: payloads, blocks and lookups by `eval`, logging the
    /// node events `Log` logs for `Plan::run`.
    fn perform(
        plan: &Plan,
        graph: &Graph<Pay>,
        eval: &mut Fan,
        task: &Task<Val>,
        events: &mut Vec<String>,
    ) -> Outcome<Val, String, String> {
        let node = graph.node(task.node);
        let branch = task.branch.as_slice();
        let lift = |result: Result<Val, String>, wrap: fn(Val) -> Outcome<Val, String, String>| {
            result.map_or_else(Outcome::Failed, wrap)
        };
        match &task.work {
            Work::Compute {
                args,
                expands,
                late,
            } => {
                if *late {
                    match eval.lookup(node, branch) {
                        Err(error) => return Outcome::Failed(error),
                        Ok(Some(value)) => {
                            let absent = node.may_be_absent && eval.is_absent(&value);
                            return Outcome::Hit { value, absent };
                        }
                        Ok(None) => {}
                    }
                }
                let mut built = Vec::new();
                for (id, arg) in args {
                    let value = match arg {
                        ArgValue::One(value) => value.clone(),
                        ArgValue::Gather(values) => match eval.gather(node, *id, values.clone()) {
                            Ok(value) => value,
                            Err(error) => return Outcome::Failed(error),
                        },
                        ArgValue::Omitted => continue,
                    };
                    built.push(Arg {
                        node: *id,
                        name: graph.node(*id).name.as_str(),
                        value,
                    });
                }
                // `Log` records the node events `Plan::run` sends a plan with an expand.
                let logs = plan.has_expands();
                if logs {
                    events.push(format!("pre:{}:{branch:?}", node.name));
                }
                let result = eval.evaluate_in(node, node.payload().unwrap(), &built, branch);
                if logs {
                    events.push(format!("post:{}:{branch:?}:{}", node.name, result.is_ok()));
                }
                match result {
                    Err(error) => Outcome::Failed(error),
                    Ok(value) => {
                        let absent = node.declines && eval.is_absent(&value);
                        if *expands && !absent {
                            lift(eval.iterate(node, &value, branch), Outcome::Iterator)
                        } else {
                            Outcome::Value { value, absent }
                        }
                    }
                }
            }
            Work::Iterate { iterable } => {
                lift(eval.iterate(node, iterable, branch), Outcome::Iterator)
            }
            Work::Pull { iterator } => eval
                .next_item(node, iterator)
                .map_or_else(Outcome::Failed, Outcome::Item),
            Work::Finish {
                iterator,
                exhausted,
            } => lift(
                eval.finish_items(node, iterator, *exhausted),
                Outcome::Finished,
            ),
            Work::Probe { block } => {
                let keys = eval.keys.clone();
                probe(plan, graph, *block, &mut Vec::new(), |id| {
                    let node = graph.node(id);
                    let key = keys.get(&(node.name.clone(), branch.to_vec())).cloned();
                    eval.lookup(node, branch).map(|hit| {
                        let hit = hit.map(|value| {
                            let absent = node.may_be_absent && eval.is_absent(&value);
                            (value, absent)
                        });
                        (hit, key)
                    })
                })
            }
        }
    }

    /// Logs what a report settled, as `Log` logs it for `Plan::run`.
    fn record(graph: &Graph<Pay>, report: &Report<Val>, events: &mut Vec<String>) {
        for (node, branch, _) in &report.hits {
            events.push(format!("hit:{}:{branch:?}", graph.node(*node).name));
        }
        for (node, branch, items) in &report.expanded {
            events.push(format!(
                "expand:{}:{branch:?}:{items}",
                graph.node(*node).name
            ));
        }
        for (node, branch, _) in &report.skipped {
            events.push(format!("skipped:{}:{branch:?}", graph.node(*node).name));
        }
    }

    /// The outputs by name.
    fn named(graph: &Graph<Pay>, outputs: Result<Outputs_, RunError<String>>) -> Outputs {
        outputs.map(|outputs| {
            outputs
                .into_iter()
                .map(|(id, value)| (graph.node(id).name.clone(), value))
                .collect()
        })
    }

    type Outputs_ = crate::run::Outputs<Val>;

    /// Runs `plan` on the scheduler with one worker, stepping one task at a time.
    fn drive(
        graph: &Graph<Pay>,
        plan: &Plan,
        inputs: &[(&str, Val)],
        overrides: &[(&str, Val)],
        eval: &mut Fan,
        events: &mut Vec<String>,
    ) -> Outputs {
        let seeded = seed(graph, plan, inputs, overrides, eval);
        let (mut scheduler, report) = Sched::new(plan, graph, seeded, Options::default());
        record(graph, &report, events);
        loop {
            match scheduler.next(plan, graph, &Unlimited, Seat::Take) {
                Next::Task(task) => {
                    let outcome = perform(plan, graph, eval, &task, events);
                    let report = scheduler.complete(plan, graph, &Unlimited, task.id, outcome);
                    record(graph, &report, events);
                }
                Next::Wait => return Err(broken("one worker waited".to_owned())),
                Next::Done => break,
            }
        }
        named(graph, scheduler.finish(plan, graph))
    }

    /// What one run gives: its outputs, its evaluator and its events.
    struct Ran {
        outputs: Outputs,
        eval: Fan,
        events: Vec<String>,
    }

    fn by_plan_run(
        graph: &Graph<Pay>,
        plan: &Plan,
        inputs: &[(&str, Val)],
        overrides: &[(&str, Val)],
        mut eval: Fan,
    ) -> Ran {
        let map = |pairs: &[(&str, Val)]| -> BTreeMap<String, Val> {
            pairs
                .iter()
                .map(|(name, value)| ((*name).to_owned(), value.clone()))
                .collect()
        };
        let mut log = Log::default();
        let outputs = plan.run(
            graph,
            &mut eval,
            &map(inputs),
            &map(overrides),
            &mut [&mut log],
        );
        Ran {
            outputs,
            eval,
            events: log.events,
        }
    }

    fn by_scheduler(
        graph: &Graph<Pay>,
        plan: &Plan,
        inputs: &[(&str, Val)],
        overrides: &[(&str, Val)],
        mut eval: Fan,
    ) -> Ran {
        let mut events = Vec::new();
        let outputs = drive(graph, plan, inputs, overrides, &mut eval, &mut events);
        Ran {
            outputs,
            eval,
            events,
        }
    }

    /// One case of the equivalence: a graph, a request and an evaluator to start from.
    struct Case {
        name: &'static str,
        graph: Graph<Pay>,
        outputs: &'static [&'static str],
        inputs: Vec<(&'static str, Val)>,
        overrides: Vec<(&'static str, Val)>,
        eval: fn() -> Fan,
    }

    impl Case {
        fn new(name: &'static str, graph: Graph<Pay>, outputs: &'static [&'static str]) -> Self {
            Self {
                name,
                graph,
                outputs,
                inputs: Vec::new(),
                overrides: Vec::new(),
                eval: Fan::default,
            }
        }

        fn inputs(self, inputs: Vec<(&'static str, Val)>) -> Self {
            Self { inputs, ..self }
        }

        fn overrides(self, overrides: Vec<(&'static str, Val)>) -> Self {
            Self { overrides, ..self }
        }

        fn eval(self, eval: fn() -> Fan) -> Self {
            Self { eval, ..self }
        }

        /// Asserts the scheduler, with one worker, does what `Plan::run` does.
        fn check(&self) {
            let names: Vec<&str> = self.inputs.iter().map(|(n, _)| *n).collect();
            let overridden: Vec<&str> = self.overrides.iter().map(|(n, _)| *n).collect();
            let plan = self
                .graph
                .prepare(&request(self.outputs, &names, &overridden))
                .unwrap();
            let want = by_plan_run(
                &self.graph,
                &plan,
                &self.inputs,
                &self.overrides,
                (self.eval)(),
            );
            let got = by_scheduler(
                &self.graph,
                &plan,
                &self.inputs,
                &self.overrides,
                (self.eval)(),
            );
            let name = self.name;
            assert_eq!(got.outputs, want.outputs, "{name}: outputs");
            assert_eq!(got.eval.calls, want.eval.calls, "{name}: calls");
            // `Plan::run` asks `lookup` of an outer late step before the absence rules skip
            // it; the scheduler skips it unasked, since nothing can be stored for it.
            let skipped: Vec<String> = got
                .events
                .iter()
                .filter_map(|e| e.strip_prefix("skipped:")?.strip_suffix(":[]"))
                .map(str::to_owned)
                .collect();
            let asked: Vec<_> = want
                .eval
                .asked
                .iter()
                .filter(|(node, branch)| !(branch.is_empty() && skipped.contains(node)))
                .cloned()
                .collect();
            assert_eq!(got.eval.asked, asked, "{name}: lookups");
            assert_eq!(got.eval.iterated, want.eval.iterated, "{name}: iterated");
            assert_eq!(got.eval.finished, want.eval.finished, "{name}: finished");
            assert_eq!(got.events, want.events, "{name}: events");
        }
    }

    /// a <- x, b <- a, c <- a, d <- b, c.
    fn diamond() -> Graph<Pay> {
        Graph::build(vec![
            node("a", &[plain("x")], tens),
            node("b", &[plain("a")], tens),
            node("c", &[plain("a")], |args| Ok(Num(num(&args[0]) + 1))),
            node("d", &[plain("b"), plain("c")], tens),
        ])
        .unwrap()
    }

    /// A chain of `n` nodes over `seed`.
    fn line(n: usize) -> Graph<Pay> {
        let specs: Vec<NodeSpec<Pay>> = (0..n)
            .map(|i| {
                let from = if i == 0 {
                    "seed".to_owned()
                } else {
                    format!("n{:05}", i - 1)
                };
                node(&format!("n{i:05}"), &[plain(&from)], |args| {
                    Ok(Num(num(&args[0]) + 1))
                })
            })
            .collect();
        Graph::build(specs).unwrap()
    }

    /// Rows of three, each node reading two of the row above.
    fn layered() -> Graph<Pay> {
        let mut specs = Vec::new();
        for layer in 0..4 {
            for column in 0..3 {
                let name = format!("l{layer}_{column}");
                let reads: Vec<String> = if layer == 0 {
                    vec!["seed".to_owned()]
                } else {
                    vec![
                        format!("l{}_{column}", layer - 1),
                        format!("l{}_{}", layer - 1, (column + 1) % 3),
                    ]
                };
                let reads: Vec<_> = reads.iter().map(|r| plain(r)).collect();
                specs.push(node(&name, &reads, tens));
            }
        }
        Graph::build(specs).unwrap()
    }

    /// `a` declines; `req` requires it, `opt` has a default for it, `adm` admits it.
    fn consumers() -> Graph<Pay> {
        Graph::build(vec![
            declines(node("a", &[], |_| Ok(Absent))),
            node("req", &[plain("a")], tens),
            node(
                "opt",
                &[("a", Dependency::Optional, None), plain("x")],
                echo,
            ),
            node(
                "adm",
                &[("a", Dependency::Admits { optional: false }, None)],
                echo,
            ),
            node("after", &[plain("req")], tens),
        ])
        .unwrap()
    }

    fn mid() -> Graph<Pay> {
        Graph::build(vec![
            expand("a", &[], |_| Ok(items(&[1, 2]))),
            node("b", &[plain("a")], tens),
            expand("mid", &[collect("b")], |args| Ok(args[0].clone())),
            node("c", &[plain("mid")], tens),
            node("out", &[collect("c")], first),
        ])
        .unwrap()
    }

    fn deep(depth: usize) -> Graph<Pay> {
        let mut specs = vec![expand("e0000", &[], |_| Ok(items(&[1])))];
        specs.extend((1..depth).map(|i| {
            expand(
                &format!("e{i:04}"),
                &[plain(&format!("e{:04}", i - 1))],
                |args| Ok(items(&[num(&args[0])])),
            )
        }));
        specs.push(node("leaf", &[plain(&format!("e{:04}", depth - 1))], first));
        specs.push(node(
            &format!("c{:04}", depth - 1),
            &[collect("leaf")],
            first,
        ));
        specs.extend((0..depth - 1).map(|i| {
            node(
                &format!("c{i:04}"),
                &[collect(&format!("c{:04}", i + 1))],
                first,
            )
        }));
        Graph::build(specs).unwrap()
    }

    fn long_block(length: usize) -> Graph<Pay> {
        let mut specs = vec![
            expand("e", &[], |_| Ok(items(&[1, 2]))),
            node("n00000", &[plain("e")], first),
        ];
        specs.extend((1..length).map(|i| {
            node(
                &format!("n{i:05}"),
                &[plain(&format!("n{:05}", i - 1))],
                first,
            )
        }));
        specs.push(node(
            "total",
            &[collect(&format!("n{:05}", length - 1))],
            first,
        ));
        Graph::build(specs).unwrap()
    }

    fn siblings(inside: bool) -> Graph<Pay> {
        let mut specs = sibling_collect(inside.then_some("outer"));
        if inside {
            specs.push(expand("outer", &[], |_| Ok(items(&[1]))));
            specs.push(node("seed", &[plain("outer")], |_| Ok(Num(0))));
            specs.push(node("all", &[collect("total")], first));
        } else {
            specs.push(node("seed", &[], |_| Ok(Num(0))));
        }
        Graph::build(specs).unwrap()
    }

    /// Cases without blocks, then with them.
    fn plain_cases() -> Vec<Case> {
        let seed = || vec![("seed", Num(1))];
        vec![
            Case::new("diamond", diamond(), &["d", "b"]).inputs(vec![("x", Num(5))]),
            Case::new("diamond override", diamond(), &["d"])
                .inputs(vec![("x", Num(5))])
                .overrides(vec![("b", Num(7))]),
            Case::new("chain", line(20), &["n00019"]).inputs(seed()),
            Case::new("layered", layered(), &["l3_0", "l3_1", "l3_2"]).inputs(seed()),
            Case::new("three rules", consumers(), &["req", "opt", "adm", "after"])
                .inputs(vec![("x", Num(5))]),
            Case::new("absent override", consumers(), &["req", "adm", "after"])
                .overrides(vec![("a", Absent)]),
        ]
    }

    fn block_cases() -> Vec<Case> {
        vec![
            Case::new("fan", fan(items(&[3, 1, 2])), &["total"]),
            Case::new("empty fan", fan(items(&[])), &["total"]),
            Case::new("expand alone", fan(items(&[4, 5])), &["picked"]),
            Case::new("expand override", fan(items(&[1])), &["total"])
                .overrides(vec![("picked", nums(&[5, 6]))]),
            Case::new("collect override", fan(items(&[1])), &["total"])
                .overrides(vec![("total", Num(7))]),
            Case::new("outer only", fan(items(&[1])), &["zz_rate"]),
            Case::new("nested", nested(), &["outer_sum"]),
            Case::new("nested hit", nested(), &["outer_sum"]).eval(|| Fan {
                hits: BTreeMap::from([hit("inner", &[1], items(&[0]))]),
                ..Fan::default()
            }),
            Case::new("nested failure", nested(), &["outer_sum"]).eval(|| Fan {
                fail: Some(("leaf", vec![0, 1])),
                ..Fan::default()
            }),
            Case::new(
                "failing iterator",
                Graph::build(vec![
                    expand("outer", &[], |_| Ok(items(&[1]))),
                    expand("inner", &[plain("outer")], |_| {
                        Ok(Val::Gen(vec![Num(1)], End::Fail))
                    }),
                    node("leaf", &[plain("inner")], tens),
                    node("inner_sum", &[collect("leaf")], first),
                    node("outer_sum", &[collect("inner_sum")], first),
                ])
                .unwrap(),
                &["outer_sum"],
            ),
            Case::new("branch hit", chain(), &["total"]).eval(|| Fan {
                hits: BTreeMap::from([hit("b", &[1], Num(-1))]),
                ..Fan::default()
            }),
            Case::new("hits in plan order", chain(), &["total", "count"]).eval(|| Fan {
                hits: BTreeMap::from([
                    hit("c", &[0], Num(5)),
                    hit("b", &[0], Num(4)),
                    hit("count", &[], Num(0)),
                ]),
                ..Fan::default()
            }),
            Case::new("outer late hit", chain(), &["total"]).eval(|| Fan {
                hits: BTreeMap::from([hit("total", &[], Num(9))]),
                ..Fan::default()
            }),
            Case::new("failing lookup", chain(), &["total"]).eval(|| Fan {
                lookup_fails: Some(("a", vec![1])),
                ..Fan::default()
            }),
            Case::new("collect present", declining_details(), &["total"]),
            Case::new("collect all", declining_details(), &["every"]),
            Case::new(
                "absent expand",
                absent_expand(Absent),
                &["req", "opt", "adm", "picked"],
            ),
            Case::new(
                "declining midway",
                absent_expand(Val::Gen(vec![Num(1), Num(2)], End::Decline)),
                &["req", "adm"],
            ),
            Case::new(
                "wrong decline",
                fan(Val::Gen(vec![Num(1)], End::Decline)),
                &["total"],
            ),
            Case::new(
                "wrong decline alone",
                fan(Val::Gen(vec![Num(1)], End::Decline)),
                &["picked"],
            ),
            Case::new(
                "absent hit",
                Graph::build(vec![
                    expand("picked", &[], |_| Ok(items(&[1, 2]))),
                    declines(node("a", &[plain("picked")], tens)),
                    node("b", &[plain("a")], tens),
                    node("total", &[collect("b")], first),
                ])
                .unwrap(),
                &["total"],
            )
            .eval(|| Fan {
                hits: BTreeMap::from([hit("a", &[0], Absent)]),
                ..Fan::default()
            }),
            Case::new("collect and expand", mid(), &["out"]),
            Case::new("sibling collect", siblings(false), &["total"]),
            Case::new("nested sibling collect", siblings(true), &["all"]),
        ]
    }

    #[test]
    fn one_worker_matches_plan_run_task_for_task() {
        for case in plain_cases().iter().chain(&block_cases()) {
            case.check();
        }
    }

    #[test]
    fn no_recursion_on_a_1000_deep_nesting_or_a_50000_chain_in_a_block() {
        let graph = deep(1_000);
        let plan = graph.prepare(&request(&["c0000"], &[], &[])).unwrap();
        let ran = by_scheduler(&graph, &plan, &[], &[], Fan::default());
        assert_eq!(num(&ran.outputs.unwrap()["c0000"]), 1);

        let graph = long_block(50_000);
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let ran = by_scheduler(&graph, &plan, &[], &[], Fan::default());
        assert_eq!(ran.outputs.unwrap()["total"], nums(&[1, 2]));
    }

    #[test]
    fn order_keys_compare_by_step_then_branch() {
        let key = |parts: &[u32]| OrderKey(parts.to_vec());
        assert!(key(&[0, 3, 1]) < key(&[0, 3, 2]));
        assert!(key(&[0, 3, 2]) < key(&[1]));
        // A pull precedes its branch's first step, which precedes the next pull.
        assert!(key(&[2, 0]) < key(&[2, 0, 0]));
        assert!(key(&[2, 0, 5]) < key(&[2, 1]));
        let graph = fan(items(&[7, 8]));
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let picked = plan.order_key(graph.id_of("picked").unwrap(), &[]);
        let tasks = every_task(&graph, &plan, Fan::default());
        let pulls: Vec<&OrderKey> = tasks
            .iter()
            .filter(|t| matches!(t.work, Work::Pull { .. }))
            .map(|t| &t.key)
            .collect();
        let items = |i: u32| key(&[picked.0[0], i]);
        assert_eq!(pulls, [&items(0), &items(1), &items(2)]);
        let details = tasks
            .iter()
            .find(|t| graph.node(t.node).name == "details")
            .unwrap();
        assert!(items(0) < details.key && details.key < items(1));
    }

    /// Every task of a one-worker run, in the order handed out.
    fn every_task(graph: &Graph<Pay>, plan: &Plan, mut eval: Fan) -> Vec<Task<Val>> {
        let seeded = seed(graph, plan, &[], &[], &eval);
        let (mut scheduler, _) = Sched::new(plan, graph, seeded, Options::default());
        let mut tasks = Vec::new();
        while let Next::Task(task) = scheduler.next(plan, graph, &Unlimited, Seat::Take) {
            let outcome = perform(plan, graph, &mut eval, &task, &mut Vec::new());
            scheduler.complete(plan, graph, &Unlimited, task.id, outcome);
            tasks.push(task);
        }
        tasks
    }

    #[test]
    fn order_key_matches_the_key_the_scheduler_gives() {
        let graph = nested();
        let plan = graph.prepare(&request(&["outer_sum"], &[], &[])).unwrap();
        let tasks = every_task(&graph, &plan, Fan::default());
        let mut steps = 0;
        for task in &tasks {
            if matches!(task.work, Work::Compute { .. } | Work::Iterate { .. }) {
                assert_eq!(
                    plan.order_key(task.node, &task.branch),
                    task.key,
                    "{task:?}"
                );
                steps += 1;
            }
        }
        assert_eq!(steps, 9);
    }

    /// The scheduler of `outputs` over `graph`, with nothing seeded.
    fn start(graph: &Graph<Pay>, outputs: &[&str], inputs: &[(&str, Val)]) -> (Plan, Sched) {
        let names: Vec<&str> = inputs.iter().map(|(n, _)| *n).collect();
        let plan = graph.prepare(&request(outputs, &names, &[])).unwrap();
        let seeded = seed(graph, &plan, inputs, &[], &Fan::default());
        let (scheduler, _) = Sched::new(&plan, graph, seeded, Options::default());
        (plan, scheduler)
    }

    fn task_of(next: Next<Val>) -> Task<Val> {
        match next {
            Next::Task(task) => task,
            other => unreachable!("expected a task, got {other:?}"),
        }
    }

    fn name_of(graph: &Graph<Pay>, task: &Task<Val>) -> String {
        graph.node(task.node).name.clone()
    }

    #[test]
    fn ready_tasks_are_handed_out_lowest_key_first() {
        let graph = diamond();
        let (plan, mut scheduler) = start(&graph, &["d"], &[("x", Num(1))]);
        let mut eval = Fan::default();
        let a = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Take));
        assert_eq!(name_of(&graph, &a), "a");
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Take),
            Next::Wait
        );
        let outcome = perform(&plan, &graph, &mut eval, &a, &mut Vec::new());
        scheduler.complete(&plan, &graph, &Unlimited, a.id, outcome);
        // `b` and `c` are both ready now, and `b` comes first in the plan.
        let b = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Take));
        let c = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Take));
        assert_eq!(
            (name_of(&graph, &b), name_of(&graph, &c)),
            ("b".to_owned(), "c".to_owned())
        );
        assert!(b.key < c.key);
    }

    /// Refuses `node` while `refusing` is set.
    struct Refuse {
        node: NodeId,
        refusing: AtomicBool,
    }

    impl Grant for Refuse {
        fn try_start(&self, node: Option<NodeId>, _seat: Seat) -> bool {
            node != Some(self.node) || !self.refusing.load(Ordering::Relaxed)
        }

        fn release(&self, _node: Option<NodeId>, _seat: Seat) {}
    }

    #[test]
    fn a_refused_task_is_skipped_for_the_next_ready_one() {
        let graph = diamond();
        let (plan, mut scheduler) = start(&graph, &["d"], &[("x", Num(1))]);
        let grant = Refuse {
            node: graph.id_of("b").unwrap(),
            refusing: AtomicBool::new(true),
        };
        let mut eval = Fan::default();
        let a = task_of(scheduler.next(&plan, &graph, &grant, Seat::Take));
        let outcome = perform(&plan, &graph, &mut eval, &a, &mut Vec::new());
        scheduler.complete(&plan, &graph, &grant, a.id, outcome);
        let c = task_of(scheduler.next(&plan, &graph, &grant, Seat::Take));
        assert_eq!(name_of(&graph, &c), "c");
        assert_eq!(
            scheduler.next(&plan, &graph, &grant, Seat::Take),
            Next::Wait
        );
        grant.refusing.store(false, Ordering::Relaxed);
        let b = task_of(scheduler.next(&plan, &graph, &grant, Seat::Take));
        assert_eq!(name_of(&graph, &b), "b");
    }

    /// Steps `plan` with as many workers as it takes: every ready task is handed out, then
    /// the one `pick` chooses of those in flight completes. Calls `watch` with the tasks in
    /// flight after each hand-out.
    fn greedy(
        graph: &Graph<Pay>,
        plan: &Plan,
        eval: &mut Fan,
        pick: impl FnMut(&[Task<Val>]) -> usize,
        watch: impl FnMut(&[Task<Val>]),
    ) -> Outputs {
        greedy_with(
            graph,
            plan,
            eval,
            Options::default(),
            pick,
            watch,
            |_, _| {},
        )
    }

    /// `greedy` with `options`, calling `seen` with each completed task and its report.
    fn greedy_with(
        graph: &Graph<Pay>,
        plan: &Plan,
        eval: &mut Fan,
        options: Options,
        mut pick: impl FnMut(&[Task<Val>]) -> usize,
        mut watch: impl FnMut(&[Task<Val>]),
        mut seen: impl FnMut(&Task<Val>, &Report<Val>),
    ) -> Outputs {
        let seeded = seed(graph, plan, &[], &[], eval);
        let (mut scheduler, _) = Sched::new(plan, graph, seeded, options);
        let mut flying: Vec<Task<Val>> = Vec::new();
        loop {
            while let Next::Task(task) = scheduler.next(plan, graph, &Unlimited, Seat::Take) {
                flying.push(task);
                watch(&flying);
            }
            if flying.is_empty() {
                break;
            }
            let task = flying.remove(pick(&flying));
            let outcome = perform(plan, graph, eval, &task, &mut Vec::new());
            let report = scheduler.complete(plan, graph, &Unlimited, task.id, outcome);
            seen(&task, &report);
        }
        named(graph, scheduler.finish(plan, graph))
    }

    /// The first task in flight that isn't `name`'s, else the first: holds `name` back.
    fn last_of(graph: &Graph<Pay>, name: &str) -> impl FnMut(&[Task<Val>]) -> usize {
        move |flying| {
            flying
                .iter()
                .position(|task| graph.node(task.node).name != name)
                .unwrap_or(0)
        }
    }

    #[test]
    fn the_in_flight_bound_holds_pulls_back() {
        let graph = fan(items(&(0..10_000).collect::<Vec<_>>()));
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let (mut pulled, mut ended, mut most) = (0_usize, 0_usize, 0_usize);
        let outputs = greedy_with(
            &graph,
            &plan,
            &mut Fan::default(),
            Options { in_flight: 4 },
            last_of(&graph, "details"),
            |_| {},
            |task, _| {
                match graph.node(task.node).name.as_str() {
                    "details" => ended += 1,
                    "picked" if matches!(task.work, Work::Pull { .. }) => pulled += 1,
                    _ => {}
                }
                most = most.max(pulled.saturating_sub(ended));
            },
        );
        assert_eq!(
            num(&outputs.unwrap()["total"]),
            (0..10_000).map(|i| 10 * i + 1).sum()
        );
        assert_eq!(most, 4);
    }

    /// `fan` over `[1, 1, 2]`, with `keys` as each branch's single-flight key of `details`.
    fn duplicates(keys: &[Option<&str>]) -> (Graph<Pay>, Plan, Fan) {
        let graph = fan(items(&[1, 1, 2]));
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let eval = Fan {
            keys: keys
                .iter()
                .enumerate()
                .filter_map(|(at, key)| {
                    Some((("details".to_owned(), vec![at]), (*key)?.to_owned()))
                })
                .collect(),
            ..Fan::default()
        };
        (graph, plan, eval)
    }

    #[test]
    fn a_duplicate_key_waits_and_completes_as_a_hit() {
        let (graph, plan, mut eval) = duplicates(&[Some("one"), Some("one"), Some("two")]);
        let mut hits = Vec::new();
        let mut most = 0;
        let outputs = greedy_with(
            &graph,
            &plan,
            &mut eval,
            Options::default(),
            last_of(&graph, "details"),
            |flying| {
                let details = flying
                    .iter()
                    .filter(|t| graph.node(t.node).name == "details");
                most = most.max(details.count());
            },
            |_, report| hits.extend(report.hits.iter().cloned()),
        );
        assert_eq!(outputs.unwrap()["total"], nums(&[11, 11, 21]));
        // Branch 1 waited for branch 0's call and got its value; branch 2's key differs.
        assert_eq!(eval.called("details"), [vec![0], vec![2]]);
        assert_eq!(hits, [(graph.id_of("details").unwrap(), vec![1], Num(11))]);
        assert_eq!(most, 2);
    }

    #[test]
    fn a_failed_original_lets_the_duplicate_run() {
        let (graph, plan, mut eval) = duplicates(&[Some("one"), Some("one"), None]);
        eval.fail = Some(("details", vec![0]));
        let mut hits = Vec::new();
        let outputs = greedy_with(
            &graph,
            &plan,
            &mut eval,
            Options::default(),
            last_of(&graph, "details"),
            |_| {},
            |_, report| hits.extend(report.hits.iter().cloned()),
        );
        // The duplicate is made ready again rather than given the failure as a hit; the
        // stopped run then hands out no compute, so the failure raised is the original's.
        assert_eq!(hits, []);
        let RunError::Node { node, branch, .. } = outputs.unwrap_err() else {
            unreachable!("a node failed");
        };
        assert_eq!(
            (node.as_str(), branch.as_slice()),
            ("details", [0].as_slice())
        );
        assert!(!eval.called("details").contains(&vec![1]));
    }

    #[test]
    fn steps_without_keys_are_never_deduplicated() {
        let (graph, plan, mut eval) = duplicates(&[None, None, None]);
        let outputs = greedy(&graph, &plan, &mut eval, last_of(&graph, "details"), |_| {});
        assert_eq!(outputs.unwrap()["total"], nums(&[11, 11, 21]));
        assert_eq!(eval.called("details"), [vec![0], vec![1], vec![2]]);
    }

    /// Steps `scheduler` with one worker until the task `stop_at` picks, which it returns
    /// unrun.
    fn until(
        scheduler: &mut Sched,
        plan: &Plan,
        graph: &Graph<Pay>,
        eval: &mut Fan,
        mut stop_at: impl FnMut(&Task<Val>) -> bool,
    ) -> Task<Val> {
        loop {
            let task = task_of(scheduler.next(plan, graph, &Unlimited, Seat::Take));
            if stop_at(&task) {
                return task;
            }
            let outcome = perform(plan, graph, eval, &task, &mut Vec::new());
            scheduler.complete(plan, graph, &Unlimited, task.id, outcome);
        }
    }

    fn failed(scheduler: &mut Sched, plan: &Plan, graph: &Graph<Pay>, task: &Task<Val>) {
        let outcome = Outcome::Failed(format!("{} failed", name_of(graph, task)));
        scheduler.complete(plan, graph, &Unlimited, task.id, outcome);
    }

    #[test]
    fn after_a_failure_only_finish_tasks_are_handed_out_innermost_first() {
        let graph = nested();
        let (plan, mut scheduler) = start(&graph, &["outer_sum"], &[]);
        let mut eval = Fan::default();
        let leaf = until(&mut scheduler, &plan, &graph, &mut eval, |task| {
            name_of(&graph, task) == "leaf"
        });
        failed(&mut scheduler, &plan, &graph, &leaf);
        assert!(scheduler.is_stopping());
        let mut closed = Vec::new();
        while let Next::Task(task) = scheduler.next(&plan, &graph, &Unlimited, Seat::Take) {
            let Work::Finish { exhausted, .. } = task.work else {
                unreachable!("only finishes after a failure, got {task:?}");
            };
            assert!(!exhausted);
            closed.push(name_of(&graph, &task));
            let outcome = perform(&plan, &graph, &mut eval, &task, &mut Vec::new());
            scheduler.complete(&plan, &graph, &Unlimited, task.id, outcome);
        }
        assert_eq!(closed, ["inner", "outer"]);
        let RunError::Node { node, branch, .. } = scheduler.finish(&plan, &graph).unwrap_err()
        else {
            unreachable!("a node failed");
        };
        assert_eq!(
            (node.as_str(), branch.as_slice()),
            ("leaf", [0, 0].as_slice())
        );
    }

    #[test]
    fn an_iterator_with_a_pull_in_flight_is_finished_after_the_pull() {
        let graph = fan(items(&[1, 2, 3]));
        let (plan, mut scheduler) = start(&graph, &["total"], &[]);
        let mut eval = Fan::default();
        // The first item's probe, with the second item's pull still to run.
        let probe = until(&mut scheduler, &plan, &graph, &mut eval, |task| {
            matches!(task.work, Work::Probe { .. })
        });
        let pull = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Take));
        assert!(matches!(pull.work, Work::Pull { .. }));
        failed(&mut scheduler, &plan, &graph, &probe);
        // The pull is in flight: its generator is not closed while it runs.
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Take),
            Next::Wait
        );
        let outcome = perform(&plan, &graph, &mut eval, &pull, &mut Vec::new());
        let report = scheduler.complete(&plan, &graph, &Unlimited, pull.id, outcome);
        // The item it gave is released, and the iterator is finished, once.
        assert_eq!(report.released, [Num(2)]);
        let finish = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Take));
        assert!(matches!(
            finish.work,
            Work::Finish {
                exhausted: false,
                ..
            }
        ));
        let outcome = perform(&plan, &graph, &mut eval, &finish, &mut Vec::new());
        scheduler.complete(&plan, &graph, &Unlimited, finish.id, outcome);
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Take),
            Next::Done
        );
        assert_eq!(eval.finished, [("picked".to_owned(), false)]);
    }

    #[test]
    fn the_lowest_order_key_failure_is_returned() {
        let graph = fan(items(&[1, 2, 3]));
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let mut eval = Fan::default();
        let seeded = seed(&graph, &plan, &[], &[], &eval);
        let (mut scheduler, _) = Sched::new(&plan, &graph, seeded, Options::default());
        let mut details = Vec::new();
        let mut flying = Vec::new();
        // Run everything but `details`, which is held back until all three are out.
        while details.len() < 3 {
            while let Next::Task(task) = scheduler.next(&plan, &graph, &Unlimited, Seat::Take) {
                if name_of(&graph, &task) == "details" {
                    details.push(task);
                } else {
                    flying.push(task);
                }
            }
            let Some(task) = flying.pop() else {
                break;
            };
            let outcome = perform(&plan, &graph, &mut eval, &task, &mut Vec::new());
            scheduler.complete(&plan, &graph, &Unlimited, task.id, outcome);
        }
        // They fail last branch first.
        for task in details.iter().rev() {
            failed(&mut scheduler, &plan, &graph, task);
        }
        loop {
            while let Next::Task(task) = scheduler.next(&plan, &graph, &Unlimited, Seat::Take) {
                flying.push(task);
            }
            let Some(task) = flying.pop() else {
                break;
            };
            let outcome = perform(&plan, &graph, &mut eval, &task, &mut Vec::new());
            scheduler.complete(&plan, &graph, &Unlimited, task.id, outcome);
        }
        let RunError::Node { node, branch, .. } = scheduler.finish(&plan, &graph).unwrap_err()
        else {
            unreachable!("a node failed");
        };
        assert_eq!(
            (node.as_str(), branch.as_slice()),
            ("details", [0].as_slice())
        );
    }

    #[test]
    fn blocked_names_the_refused_node() {
        let graph = diamond();
        let (plan, mut scheduler) = start(&graph, &["d"], &[("x", Num(1))]);
        let a = graph.id_of("a").unwrap();
        let grant = Refuse {
            node: a,
            refusing: AtomicBool::new(true),
        };
        assert_eq!(scheduler.blocked(), None);
        assert_eq!(
            scheduler.next(&plan, &graph, &grant, Seat::Take),
            Next::Wait
        );
        assert_eq!(scheduler.blocked(), Some(a));
        grant.refusing.store(false, Ordering::Relaxed);
        let task = task_of(scheduler.next(&plan, &graph, &grant, Seat::Take));
        assert_eq!(scheduler.blocked(), None);
        let outcome = perform(&plan, &graph, &mut Fan::default(), &task, &mut Vec::new());
        scheduler.complete(&plan, &graph, &grant, task.id, outcome);
        // `b` refused while `c` runs is not blocked: something of the run is in flight.
        let refuse_b = Refuse {
            node: graph.id_of("b").unwrap(),
            refusing: AtomicBool::new(true),
        };
        let c = task_of(scheduler.next(&plan, &graph, &refuse_b, Seat::Take));
        assert_eq!(name_of(&graph, &c), "c");
        assert_eq!(
            scheduler.next(&plan, &graph, &refuse_b, Seat::Take),
            Next::Wait
        );
        assert_eq!(scheduler.blocked(), None);
    }

    #[test]
    fn a_lent_seat_is_held_by_one_task_at_a_time_and_reused() {
        let graph = diamond();
        let (plan, mut scheduler) = start(&graph, &["d"], &[("x", Num(1))]);
        let mut eval = Fan::default();
        // No seat is lent yet.
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Lent),
            Next::Wait
        );
        scheduler.lend();
        let a = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Lent));
        let outcome = perform(&plan, &graph, &mut eval, &a, &mut Vec::new());
        scheduler.complete(&plan, &graph, &Unlimited, a.id, outcome);
        // `b` and `c` are ready, but the lent seat holds one task at a time.
        let b = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Lent));
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Lent),
            Next::Wait
        );
        let outcome = perform(&plan, &graph, &mut eval, &b, &mut Vec::new());
        scheduler.complete(&plan, &graph, &Unlimited, b.id, outcome);
        let c = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Lent));
        assert_eq!(name_of(&graph, &c), "c");
        let outcome = perform(&plan, &graph, &mut eval, &c, &mut Vec::new());
        scheduler.complete(&plan, &graph, &Unlimited, c.id, outcome);
        let d = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Lent));
        let outcome = perform(&plan, &graph, &mut eval, &d, &mut Vec::new());
        scheduler.complete(&plan, &graph, &Unlimited, d.id, outcome);
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Lent),
            Next::Done
        );
        assert_eq!(
            named(&graph, scheduler.finish(&plan, &graph)).unwrap()["d"],
            Num(1110)
        );
    }

    #[test]
    fn limits_cap_workers_nodes_and_groups_across_two_schedulers() {
        let graph = fan(items(&[1, 2, 3, 4, 5, 6]));
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let id = |name: &str| graph.id_of(name).unwrap();
        let mut caps = vec![None; graph.nodes().len()];
        caps[id("details").index()] = Some(1);
        let mut groups = vec![None; graph.nodes().len()];
        groups[id("picked").index()] = Some(0);
        groups[id("zz_rate").index()] = Some(0);
        let limits = Limits::new(Some(2), caps, groups, vec![1]);
        let mut evals = [Fan::default(), Fan::default()];
        let mut schedulers: Vec<Sched> = evals
            .iter()
            .map(|eval| {
                let seeded = seed(&graph, &plan, &[], &[], eval);
                Sched::new(&plan, &graph, seeded, Options::default()).0
            })
            .collect();
        let mut flying: Vec<(usize, Task<Val>)> = Vec::new();
        let mut most = (0, 0, 0);
        loop {
            for (at, scheduler) in schedulers.iter_mut().enumerate() {
                while let Next::Task(task) = scheduler.next(&plan, &graph, &limits, Seat::Take) {
                    flying.push((at, task));
                }
            }
            let calls = |names: &[&str]| {
                flying
                    .iter()
                    .filter(|(_, t)| matches!(t.work, Work::Compute { .. } | Work::Pull { .. }))
                    .filter(|(_, t)| names.contains(&name_of(&graph, t).as_str()))
                    .count()
            };
            let now = (
                flying.len(),
                calls(&["details"]),
                calls(&["picked", "zz_rate"]),
            );
            most = (most.0.max(now.0), most.1.max(now.1), most.2.max(now.2));
            if flying.is_empty() {
                break;
            }
            let (at, task) = flying.remove(0);
            let outcome = perform(&plan, &graph, &mut evals[at], &task, &mut Vec::new());
            schedulers[at].complete(&plan, &graph, &limits, task.id, outcome);
        }
        assert_eq!(most, (2, 1, 1));
        for scheduler in schedulers {
            let outputs = named(&graph, scheduler.finish(&plan, &graph)).unwrap();
            assert_eq!(outputs["total"], nums(&[11, 21, 31, 41, 51, 61]));
        }
        assert_eq!(limits.held(LimitSlot::Worker), 0);
    }

    #[test]
    fn one_pull_per_iterator_in_flight() {
        let graph = nested();
        let plan = graph.prepare(&request(&["outer_sum"], &[], &[])).unwrap();
        let mut most = 0;
        let outputs = greedy(
            &graph,
            &plan,
            &mut Fan::default(),
            |_| 0,
            |flying| {
                let mut pulls: Vec<(NodeId, &[usize])> = flying
                    .iter()
                    .filter(|t| matches!(t.work, Work::Pull { .. }))
                    .map(|t| (t.node, t.branch.as_slice()))
                    .collect();
                let count = pulls.len();
                most = most.max(count);
                pulls.sort_unstable();
                pulls.dedup();
                assert_eq!(pulls.len(), count, "two pulls of one iterator: {flying:?}");
            },
        );
        assert_eq!(
            outputs.unwrap()["outer_sum"],
            List(vec![nums(&[20, 21]), nums(&[10])])
        );
        // The pulls of the outer and the inner iterators overlap.
        assert!(most >= 2);
    }

    #[test]
    fn a_collect_waits_for_every_branch_and_gathers_in_branch_order() {
        let graph = fan(items(&[3, 1, 2, 5]));
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let mut eval = Fan::default();
        // The last task handed out completes first, so the branches complete in reverse.
        let outputs = greedy(&graph, &plan, &mut eval, |flying| flying.len() - 1, |_| {});
        assert_eq!(outputs.unwrap()["total"], nums(&[31, 11, 21, 51]));
        let order: Vec<Vec<usize>> = eval.called("details");
        assert_eq!(order, [vec![3], vec![2], vec![1], vec![0]]);
        assert_eq!(eval.calls.last().unwrap().0, "total");
    }

    #[test]
    fn a_probe_hit_skips_the_branch_upstream_and_is_reported() {
        let graph = chain();
        let (plan, mut scheduler) = start(&graph, &["total"], &[]);
        let mut eval = Fan {
            hits: BTreeMap::from([hit("b", &[1], Num(-1))]),
            ..Fan::default()
        };
        let mut hits = Vec::new();
        while let Next::Task(task) = scheduler.next(&plan, &graph, &Unlimited, Seat::Take) {
            let outcome = perform(&plan, &graph, &mut eval, &task, &mut Vec::new());
            let report = scheduler.complete(&plan, &graph, &Unlimited, task.id, outcome);
            hits.extend(report.hits);
        }
        assert_eq!(hits, [(graph.id_of("b").unwrap(), vec![1], Num(-1))]);
        assert_eq!(eval.called("a"), [vec![0], vec![2]]);
        let outputs = named(&graph, scheduler.finish(&plan, &graph));
        assert_eq!(
            outputs.unwrap()["total"],
            List(vec![Num(100), Num(-1), Num(300)])
        );
    }

    #[test]
    fn a_late_outer_hit_completes_without_a_call() {
        let graph = chain();
        let (plan, mut scheduler) = start(&graph, &["total"], &[]);
        let mut eval = Fan::default();
        loop {
            let task = task_of(scheduler.next(&plan, &graph, &Unlimited, Seat::Take));
            if name_of(&graph, &task) == "total" {
                assert!(matches!(task.work, Work::Compute { late: true, .. }));
                let report = scheduler.complete(
                    &plan,
                    &graph,
                    &Unlimited,
                    task.id,
                    Outcome::Hit {
                        value: Num(9),
                        absent: false,
                    },
                );
                assert_eq!(report.hits, [(task.node, Vec::new(), Num(9))]);
                break;
            }
            assert!(!matches!(task.work, Work::Compute { late: true, .. }));
            let outcome = perform(&plan, &graph, &mut eval, &task, &mut Vec::new());
            scheduler.complete(&plan, &graph, &Unlimited, task.id, outcome);
        }
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Take),
            Next::Done
        );
        assert_eq!(eval.called("total"), [] as [Vec<usize>; 0]);
        let outputs = named(&graph, scheduler.finish(&plan, &graph));
        assert_eq!(outputs.unwrap()["total"], Num(9));
    }

    #[test]
    fn skips_follow_the_three_rules_and_are_reported_with_keys() {
        let graph = consumers();
        let names = ["req", "opt", "adm", "after"];
        let plan = graph.prepare(&request(&names, &["x"], &[])).unwrap();
        let mut eval = Fan::default();
        let mut skipped = Vec::new();
        let mut arguments = BTreeMap::new();
        let seeded = seed(&graph, &plan, &[("x", Num(5))], &[], &eval);
        let (mut scheduler, _) = Sched::new(&plan, &graph, seeded, Options::default());
        while let Next::Task(task) = scheduler.next(&plan, &graph, &Unlimited, Seat::Take) {
            if let Work::Compute { args, .. } = &task.work {
                arguments.insert(name_of(&graph, &task), args.clone());
            }
            let outcome = perform(&plan, &graph, &mut eval, &task, &mut Vec::new());
            let report = scheduler.complete(&plan, &graph, &Unlimited, task.id, outcome);
            skipped.extend(report.skipped);
        }
        let id = |name: &str| graph.id_of(name).unwrap();
        // `req` requires the absent `a`, and `after` requires the skipped `req`.
        let key = |name: &str| plan.order_key(id(name), &[]);
        assert_eq!(
            skipped,
            [
                (id("req"), Vec::new(), key("req")),
                (id("after"), Vec::new(), key("after")),
            ]
        );
        assert_eq!(arguments["adm"], [(id("a"), ArgValue::One(Absent))]);
        assert_eq!(
            arguments["opt"],
            [
                (id("a"), ArgValue::Omitted),
                (id("x"), ArgValue::One(Num(5)))
            ]
        );
        assert!(!arguments.contains_key("req") && !arguments.contains_key("after"));
        let outputs = named(&graph, scheduler.finish(&plan, &graph)).unwrap();
        assert_eq!(outputs["req"], Absent);
        assert_eq!(outputs["after"], Absent);
    }

    thread_local! {
        /// Set while the scheduler runs, so a value dropped inside it is seen.
        static INSIDE: Cell<bool> = const { Cell::new(false) };
    }

    /// A number that counts its live copies and the ones dropped inside the scheduler.
    #[derive(Debug)]
    struct Watched {
        value: i64,
        live: Rc<Cell<usize>>,
        inside: Rc<Cell<usize>>,
    }

    impl Watched {
        fn new(value: i64, like: &Self) -> Self {
            like.live.set(like.live.get() + 1);
            Self {
                value,
                live: Rc::clone(&like.live),
                inside: Rc::clone(&like.inside),
            }
        }
    }

    impl Clone for Watched {
        fn clone(&self) -> Self {
            Self::new(self.value, self)
        }
    }

    impl Drop for Watched {
        fn drop(&mut self) {
            self.live.set(self.live.get() - 1);
            if INSIDE.with(Cell::get) {
                self.inside.set(self.inside.get() + 1);
            }
        }
    }

    /// Runs `f` with `INSIDE` set.
    fn inside<T>(f: impl FnOnce() -> T) -> T {
        INSIDE.with(|flag| flag.set(true));
        let result = f();
        INSIDE.with(|flag| flag.set(false));
        result
    }

    #[test]
    fn released_values_are_returned_to_the_caller() {
        let graph = fan(items(&[1, 2, 3]));
        let plan = graph.prepare(&request(&["total"], &[], &[])).unwrap();
        let seed = Watched {
            value: 0,
            live: Rc::new(Cell::new(1)),
            inside: Rc::new(Cell::new(0)),
        };
        let mut seeded: Vec<Option<Slot<Watched>>> =
            (0..graph.nodes().len()).map(|_| None).collect();
        // An unplanned value: released, never dropped inside.
        seeded[0] = Some(Slot {
            value: seed.clone(),
            absent: false,
        });
        let (mut scheduler, report) = inside(|| {
            Scheduler::<Watched, String, String>::new(&plan, &graph, seeded, Options::default())
        });
        let mut released = report.released.len();
        drop(report);
        let mut pulled = 0;
        let mut most = 0;
        loop {
            let next = inside(|| scheduler.next(&plan, &graph, &Unlimited, Seat::Take));
            let Next::Task(task) = next else {
                break;
            };
            let outcome = match task.work {
                Work::Compute { args, expands, .. } => {
                    let sum: i64 = args
                        .iter()
                        .map(|(_, arg)| match arg {
                            ArgValue::One(value) => value.value,
                            ArgValue::Gather(values) => values.iter().map(|v| v.value).sum(),
                            ArgValue::Omitted => 0,
                        })
                        .sum();
                    if expands {
                        Outcome::Iterator(Watched::new(0, &seed))
                    } else {
                        Outcome::Value {
                            value: Watched::new(sum + 10, &seed),
                            absent: false,
                        }
                    }
                }
                Work::Pull { .. } if pulled < 3 => {
                    pulled += 1;
                    Outcome::Item(Item::Next(Watched::new(pulled, &seed)))
                }
                Work::Pull { .. } => Outcome::Item(Item::Done),
                Work::Finish { .. } => Outcome::Finished(Watched::new(3, &seed)),
                Work::Probe { .. } => Outcome::Needs {
                    hits: Vec::new(),
                    keys: Vec::new(),
                },
                Work::Iterate { .. } => unreachable!("nothing is overridden"),
            };
            let report = inside(|| scheduler.complete(&plan, &graph, &Unlimited, task.id, outcome));
            released += report.released.len();
            drop(report);
            most = most.max(seed.live.get());
        }
        let outputs = inside(|| scheduler.finish(&plan, &graph)).unwrap();
        assert_eq!(
            seed.inside.get(),
            0,
            "a value was dropped inside the scheduler"
        );
        assert_eq!(outputs[0].1.value, 10 + (11 + 12 + 13) + 10 * 3);
        // The unplanned seed, three items, the iterator, three `details`, the finished items
        // and `zz_rate`, and nothing is left but the seed and the output.
        assert!(released >= 9, "{released}");
        drop(outputs);
        assert_eq!(seed.live.get(), 1);
        assert!(most <= 8, "{most} values alive at once");
    }

    #[test]
    fn an_expand_requested_alone_finishes_its_items_as_its_value() {
        let graph = fan(items(&[4, 5]));
        let plan = graph.prepare(&request(&["picked"], &[], &[])).unwrap();
        let mut events = Vec::new();
        let mut eval = Fan::default();
        let outputs = drive(&graph, &plan, &[], &[], &mut eval, &mut events);
        assert_eq!(outputs.unwrap()["picked"], nums(&[4, 5]));
        assert_eq!(events.last().unwrap(), "expand:picked:[]:2");
        let tasks = every_task(&graph, &plan, Fan::default());
        assert!(!tasks.iter().any(|t| matches!(t.work, Work::Probe { .. })));
        assert_eq!(eval.finished, [("picked".to_owned(), true)]);
    }

    #[test]
    fn a_plan_from_another_graph_is_a_broken_plan_error() {
        let planned = Graph::build(vec![node("a", &[plain("x")], tens)]).unwrap();
        let plan = planned.prepare(&request(&["a"], &["x"], &[])).unwrap();
        // The same ids in other roles: `a` is an input here, and `b` has the id `x` had.
        let other = Graph::build(vec![node("b", &[plain("a")], tens)]).unwrap();
        let seeded = seed(&other, &plan, &[("b", Num(5))], &[], &Fan::default());
        let (mut scheduler, _) = Sched::new(&plan, &other, seeded, Options::default());
        assert_eq!(
            scheduler.next(&plan, &other, &Unlimited, Seat::Take),
            Next::Done
        );
        assert_eq!(
            scheduler.finish(&plan, &other).unwrap_err(),
            broken("input node 'a' planned to compute".to_owned())
        );
        // A plan other than the one it was made with.
        let graph = diamond();
        let (plan, mut scheduler) = start(&graph, &["d"], &[("x", Num(1))]);
        let shorter = graph.prepare(&request(&["a"], &["x"], &[])).unwrap();
        assert_eq!(
            scheduler.next(&shorter, &graph, &Unlimited, Seat::Take),
            Next::Done
        );
        assert_eq!(scheduler.finish(&plan, &graph).unwrap_err(), another_plan());
        // A plan that reads ids the graph doesn't have.
        let small = Graph::build(vec![node("q", &[], tens)]).unwrap();
        let (mut scheduler, _) = Sched::new(&plan, &small, vec![None], Options::default());
        assert_eq!(
            scheduler.next(&plan, &small, &Unlimited, Seat::Take),
            Next::Done
        );
        assert!(matches!(
            scheduler.finish(&plan, &small),
            Err(RunError::BrokenPlan { .. })
        ));
    }

    #[test]
    fn a_missing_seeded_value_is_reported_before_anything_runs() {
        let graph = diamond();
        let plan = graph.prepare(&request(&["d"], &["x"], &[])).unwrap();
        let (mut scheduler, _) = Sched::new(
            &plan,
            &graph,
            vec![None; graph.nodes().len()],
            Options::default(),
        );
        assert_eq!(
            scheduler.next(&plan, &graph, &Unlimited, Seat::Take),
            Next::Done
        );
        assert_eq!(
            scheduler.finish(&plan, &graph).unwrap_err(),
            RunError::MissingValues(vec!["x".to_owned()])
        );
    }
}
