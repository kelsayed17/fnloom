//! Running a plan: the executor and the two seams an embedder fills in.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use crate::error::quoted;
use crate::graph::Graph;
use crate::node::{Dependency, Node, NodeId};
use crate::plan::{Action, Plan, Step};

/// A run's outputs by node id, in `Plan::outputs` order, as `Plan::run_by_id` returns them.
pub type Outputs<V> = Vec<(NodeId, V)>;

/// Computes one node's value from its arguments; the embedder's seam for running payloads.
pub trait Evaluate<P> {
    /// The runtime value flowing along edges.
    type Value: Clone;
    /// The error a node's payload can raise.
    type Error;

    /// Runs `payload`, the payload of the function node `node`, on `args`, sorted by input id
    /// (equivalently by name).
    ///
    /// # Errors
    ///
    /// Whatever the payload raises; the executor wraps it in `RunError::Node`.
    fn evaluate(
        &mut self,
        node: &Node<P>,
        payload: &P,
        args: &[Arg<'_, Self::Value>],
    ) -> Result<Self::Value, Self::Error>;

    /// Whether `value` is absent: a node declined to produce one.
    ///
    /// Asked only of a value a node that declines (`Node::declines`) returned, and of an
    /// override of a node that may be absent (`Node::may_be_absent`); an input is never
    /// absent. The default says no value is, so an embedder whose nodes never decline needs
    /// nothing here.
    fn is_absent(&self, _value: &Self::Value) -> bool {
        false
    }

    /// The evaluator's blocks: how it iterates an expand's value and gathers a collect.
    ///
    /// The default has none. A plan with an expand step, run by an evaluator without
    /// blocks, fails with `RunError::BrokenPlan` at that step, never yielding empty blocks.
    fn blocks(&mut self) -> Option<&mut dyn Blocks<P, Self::Value, Self::Error>> {
        None
    }

    /// Runs `payload` as `evaluate` does, in the branch `branch`: one item index per level
    /// of nesting, outermost first, and empty outside every block.
    ///
    /// The executor calls this, not `evaluate`, when a plan has an expand step. The default
    /// calls `evaluate`.
    ///
    /// # Errors
    ///
    /// Whatever the payload raises; the executor wraps it in `RunError::Node`.
    fn evaluate_in(
        &mut self,
        node: &Node<P>,
        payload: &P,
        args: &[Arg<'_, Self::Value>],
        _branch: &[usize],
    ) -> Result<Self::Value, Self::Error> {
        self.evaluate(node, payload, args)
    }

    /// A value for `node` in `branch` known without computing it, such as a cache hit.
    ///
    /// Asked only for a computed step downstream of an expand (`Plan::is_late`), whose cache
    /// key is known only once the items are. Inside a branch, the steps are asked in
    /// reverse need order before the branch runs, from the nodes its collects read: a hit
    /// is not computed, and the steps only it needed are neither asked nor computed. Outside
    /// a block, a step is asked when it is reached, and a hit skips only that step. A hit
    /// for a node that may be absent is asked `is_absent`, as a computed value is. The
    /// default knows none.
    ///
    /// # Errors
    ///
    /// An error fails the run as the node's own failure.
    fn lookup(
        &mut self,
        _node: &Node<P>,
        _branch: &[usize],
    ) -> Result<Option<Self::Value>, Self::Error> {
        Ok(None)
    }
}

/// What an expand's iterator gave: an item, the end, or a decline while yielding.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Item<V> {
    /// The next item, which runs one branch of the block.
    Next(V),
    /// The iterator is exhausted.
    Done,
    /// The iterator declined after yielding: the expand is absent, holding this value, and
    /// the branches already run are dropped.
    Declined(V),
}

/// How an evaluator runs blocks: it iterates expands and gathers collects.
///
/// `Evaluate::blocks` lends it out for one call at a time, so an iterator may live in the
/// evaluator's own values and interleave with the branches it feeds.
pub trait Blocks<P, V, E> {
    /// An iterator over `iterable`: a computed expand's value, or an expand override.
    ///
    /// # Errors
    ///
    /// An error fails the run as the expand's own failure.
    fn iterate(&mut self, node: &Node<P>, iterable: &V) -> Result<V, E>;

    /// The next item; `Done` when exhausted; `Declined(v)` when the iterator declined while
    /// yielding.
    ///
    /// # Errors
    ///
    /// An error fails the run as the expand's own failure.
    fn next_item(&mut self, node: &Node<P>, iterator: &V) -> Result<Item<V>, E>;

    /// Ends the iterator: `exhausted`, or stopped early, by a failure, so it should be
    /// closed. Returns the expand node's value, which a requested expand returns. Not called
    /// after `Declined`. On a failure it is called for every open iterator, innermost first,
    /// and what it returns is dropped.
    ///
    /// # Errors
    ///
    /// An error fails the run as the expand's own failure.
    fn finish_items(&mut self, node: &Node<P>, iterator: &V, exhausted: bool) -> Result<V, E>;

    /// One collect argument of `consumer`, from the branches' values of `source`, in branch
    /// order.
    ///
    /// # Errors
    ///
    /// An error fails the run as the consumer's own failure.
    fn gather(&mut self, consumer: &Node<P>, source: NodeId, values: Vec<V>) -> Result<V, E>;
}

/// One argument passed to a node: the input it comes from and that input's value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Arg<'g, V> {
    /// The input node, so an embedder can key per-node data by `NodeId::index`.
    pub node: NodeId,
    /// The input node's name, which is the parameter the value binds to.
    pub name: &'g str,
    /// The input's value.
    pub value: V,
}

/// Lifecycle hooks around a run; every observer sees every event, in slice order.
///
/// `V` is the evaluator's value type and `E` its error type, so the node events can carry
/// a node's arguments and its result or error, all by reference: an observer that ignores
/// them costs nothing.
pub trait Observer<V, E> {
    /// Called once before anything runs.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn pre_graph(&mut self) -> Result<(), E> {
        Ok(())
    }

    /// Called before a computed node is evaluated, with the arguments it will get, sorted
    /// by input id as `Evaluate::evaluate` gets them.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn pre_node(&mut self, _node: &str, _args: &[Arg<'_, V>]) -> Result<(), E> {
        Ok(())
    }

    /// Called after a computed node was evaluated, with the value it returned or the
    /// error it raised.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn post_node(&mut self, _node: &str, _outcome: Result<&V, &E>) -> Result<(), E> {
        Ok(())
    }

    /// Called instead of the node events when a computed node is skipped, because a
    /// `Required` argument is absent: the node is then absent too. `branch` is empty.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn skipped(&mut self, _node: &str, _branch: &[usize]) -> Result<(), E> {
        Ok(())
    }

    /// `pre_node` in a branch, for a plan with an expand step; the default calls `pre_node`.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn pre_node_in(&mut self, node: &str, _branch: &[usize], args: &[Arg<'_, V>]) -> Result<(), E> {
        self.pre_node(node, args)
    }

    /// `post_node` in a branch, for a plan with an expand step; the default calls
    /// `post_node`.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn post_node_in(
        &mut self,
        node: &str,
        _branch: &[usize],
        outcome: Result<&V, &E>,
    ) -> Result<(), E> {
        self.post_node(node, outcome)
    }

    /// Called when an expand's items are exhausted, with how many there were; `branch` is
    /// the branch the expand ran in. Not called for an expand that is absent.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn post_expand(&mut self, _node: &str, _branch: &[usize], _items: usize) -> Result<(), E> {
        Ok(())
    }

    /// Called instead of the node events for a step whose value `Evaluate::lookup` gave.
    /// In a branch, the hits fire in plan order before its first step runs.
    ///
    /// # Errors
    ///
    /// An error aborts the run after every observer has seen the event.
    fn hit_in(&mut self, _node: &str, _branch: &[usize], _value: &V) -> Result<(), E> {
        Ok(())
    }

    /// Called once after the run ended; `ok` says whether it succeeded.
    ///
    /// # Errors
    ///
    /// An error is reported with the run's outcome, never instead of it.
    fn post_graph(&mut self, _ok: bool) -> Result<(), E> {
        Ok(())
    }
}

/// Why a run did not produce its outputs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunError<E> {
    /// A node's payload failed.
    Node {
        /// The failing node.
        node: String,
        /// What the payload raised.
        source: E,
        /// Errors observers raised while handling this failure (`post_node` with the error, then
        /// `post_graph(false)`); empty when they all succeeded.
        observer_errors: Vec<E>,
        /// The branch it failed in, one item index per level of nesting, outermost first;
        /// empty outside every block.
        branch: Vec<usize>,
        /// The expand of the innermost block it is in; `None` outside every block.
        block: Option<String>,
    },
    /// One or more observers failed on an event.
    Observers {
        /// The first failing event: `pre_graph`, `pre_node`, `post_node`, `skipped`,
        /// `post_expand`, `hit` or `post_graph`.
        event: &'static str,
        /// Every error raised, in observer order, then any from the closing `post_graph(false)`.
        errors: Vec<E>,
    },
    /// The plan expects values the caller did not provide; names sorted.
    ///
    /// Raised only by the check before anything runs, so no hook has fired.
    MissingValues(Vec<String>),
    /// A step found the slot table in a state the plan rules out, such as an argument
    /// with no value; a bug in the plan or a plan run on another graph.
    BrokenPlan {
        /// What was found, naming the node.
        detail: String,
        /// Errors observers raised in the closing `post_graph(false)`; empty when they all
        /// succeeded.
        observer_errors: Vec<E>,
    },
}

impl<E: fmt::Display> fmt::Display for RunError<E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Node {
                node,
                source,
                branch,
                block,
                ..
            } => match block {
                Some(block) => write!(
                    f,
                    "node '{node}' failed in branch {} of '{block}': {source}",
                    branch_text(branch)
                ),
                None => write!(f, "node '{node}' failed: {source}"),
            },
            Self::Observers { event, errors } => {
                write!(f, "{} observer error(s) in {event}", errors.len())
            }
            Self::MissingValues(names) => write!(f, "missing values: {}", quoted(names, ", ")),
            Self::BrokenPlan { detail, .. } => write!(f, "broken plan: {detail}"),
        }
    }
}

/// A branch path as Python writes a tuple: `(2,)`, `(0, 1)`.
pub(crate) fn branch_text(branch: &[usize]) -> String {
    if let [only] = branch {
        return format!("({only},)");
    }
    let parts: Vec<String> = branch.iter().map(ToString::to_string).collect();
    format!("({})", parts.join(", "))
}

impl<E: Error + 'static> Error for RunError<E> {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Node { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// The observers of one run, driven as a group.
pub(crate) struct Hooks<'a, 'o, V, E> {
    pub(crate) observers: &'a mut [&'o mut dyn Observer<V, E>],
}

impl<V, E> Hooks<'_, '_, V, E> {
    /// Delivers one event to every observer in slice order and returns the errors raised.
    pub(crate) fn fire(
        &mut self,
        mut event: impl FnMut(&mut dyn Observer<V, E>) -> Result<(), E>,
    ) -> Vec<E> {
        self.observers
            .iter_mut()
            .filter_map(|observer| event(&mut **observer).err())
            .collect()
    }

    /// Delivers one event like `fire`, failing with `RunError::Observers` if any observer did.
    pub(crate) fn check(
        &mut self,
        name: &'static str,
        event: impl FnMut(&mut dyn Observer<V, E>) -> Result<(), E>,
    ) -> Result<(), RunError<E>> {
        let errors = self.fire(event);
        if errors.is_empty() {
            Ok(())
        } else {
            Err(RunError::Observers {
                event: name,
                errors,
            })
        }
    }
}

impl<E> RunError<E> {
    /// Adds errors raised by the closing `post_graph(false)` to a failure already in hand.
    fn with_closing(mut self, closing: Vec<E>) -> Self {
        match &mut self {
            Self::Node {
                observer_errors, ..
            }
            | Self::BrokenPlan {
                observer_errors, ..
            } => observer_errors.extend(closing),
            Self::Observers { errors, .. } => errors.extend(closing),
            // Raised before any hook ran, so there is no closing event to report.
            Self::MissingValues(_) => {}
        }
        self
    }
}

impl Plan {
    /// Executes the plan; the result holds exactly the requested outputs, keyed by name.
    ///
    /// `graph` must be the graph whose `prepare` returned this plan: the plan addresses
    /// nodes by id. Passing any other graph is a caller bug. The run then reads the wrong
    /// nodes, and may panic on an id the graph does not have (see `Graph::node`) or fail
    /// with `RunError::BrokenPlan`.
    ///
    /// Steps run in plan order. Hooks fire only around `Compute` steps. The first failure
    /// aborts the run once every observer has seen the current event; `post_graph(false)` then
    /// runs, and its errors are reported with the failure.
    ///
    /// A plan with an expand step (`Plan::has_expands`) runs its blocks in reference order:
    /// at an expand step it pulls item 0, runs that branch's sub-plan to its end, pulls the
    /// next, and so on, then goes on to the next step. Nested blocks repeat this inside a
    /// branch. Such a plan calls `Evaluate::evaluate_in` and the observers' `_in` events,
    /// with the branch path. On a failure, every open iterator is finished as stopped,
    /// innermost first, before the error is returned.
    ///
    /// # Errors
    ///
    /// `RunError::MissingValues` before anything runs when a map lacks a name the plan reads;
    /// otherwise the first failure as described on `RunError`. Extra keys in the maps are
    /// ignored.
    pub fn run<P, V: Evaluate<P>>(
        &self,
        graph: &Graph<P>,
        eval: &mut V,
        inputs: &BTreeMap<String, V::Value>,
        overrides: &BTreeMap<String, V::Value>,
        observers: &mut [&mut dyn Observer<V::Value, V::Error>],
    ) -> Result<BTreeMap<String, V::Value>, RunError<V::Error>> {
        let lookup = |action, id| {
            let values = if action == Action::UseOverride {
                overrides
            } else {
                inputs
            };
            values.get(&graph.node(id).name).cloned()
        };
        let outputs = self.run_by_id(graph, eval, lookup, observers)?;
        Ok(outputs
            .into_iter()
            .map(|(id, value)| (graph.node(id).name.clone(), value))
            .collect())
    }

    /// Executes the plan as `run` does, reading inputs and overrides through `lookup`, and
    /// returns the outputs by node id, in `outputs` order.
    ///
    /// `lookup` is called once for each `UseInput` and `UseOverride` step, with its action
    /// and node, before anything runs; `None` means the value is missing. An embedder that
    /// keys its values by node needs no name strings to run a plan.
    ///
    /// # Errors
    ///
    /// `RunError::MissingValues` before anything runs when `lookup` has no value for a
    /// step; otherwise the first failure as described on `RunError`.
    pub fn run_by_id<P, V: Evaluate<P>>(
        &self,
        graph: &Graph<P>,
        eval: &mut V,
        lookup: impl FnMut(Action, NodeId) -> Option<V::Value>,
        observers: &mut [&mut dyn Observer<V::Value, V::Error>],
    ) -> Result<Outputs<V::Value>, RunError<V::Error>> {
        let slots = self.seed(graph, lookup)?;
        let mut hooks = Hooks { observers };
        let outcome = hooks
            .check("pre_graph", |observer| observer.pre_graph())
            .and_then(|()| {
                if self.has_expands() {
                    crate::branch::execute(self, graph, eval, slots, &mut hooks)
                } else if self.needs_rules() {
                    self.execute_by_rules(graph, eval, slots, &mut hooks)
                } else {
                    self.execute(graph, eval, slots, &mut hooks)
                }
            });
        match outcome {
            Ok(values) => hooks
                .check("post_graph", |observer| observer.post_graph(true))
                .map(|()| values),
            Err(failure) => {
                let closing = hooks.fire(|observer| observer.post_graph(false));
                Err(failure.with_closing(closing))
            }
        }
    }

    /// The slot table with every input and override value placed, by node id.
    fn seed<P, T: Clone, E>(
        &self,
        graph: &Graph<P>,
        mut lookup: impl FnMut(Action, NodeId) -> Option<T>,
    ) -> Result<Vec<Option<T>>, RunError<E>> {
        let mut slots = vec![None; graph.nodes().len()];
        let mut missing = Vec::new();
        for step in self.steps() {
            if !matches!(step.action, Action::UseInput | Action::UseOverride) {
                continue;
            }
            match (
                lookup(step.action, step.node),
                slots.get_mut(step.node.index()),
            ) {
                (Some(value), Some(slot)) => *slot = Some(value),
                _ => missing.push(graph.node(step.node).name.clone()),
            }
        }
        if missing.is_empty() {
            Ok(slots)
        } else {
            missing.sort();
            Err(RunError::MissingValues(missing))
        }
    }

    // The plain loop and its `compute` are inlined into `run_by_id`, and the absence-aware
    // loop is kept out of it: the instruction-count gate measures the plain loop, which the
    // compiler otherwise stops inlining once `run_by_id` holds both.
    #[expect(
        clippy::inline_always,
        reason = "the instruction-count gate measures the plain loop, which must not gain a call"
    )]
    #[inline(always)]
    fn execute<P, V: Evaluate<P>>(
        &self,
        graph: &Graph<P>,
        eval: &mut V,
        mut slots: Vec<Option<V::Value>>,
        hooks: &mut Hooks<'_, '_, V::Value, V::Error>,
    ) -> Result<Outputs<V::Value>, RunError<V::Error>> {
        for step in self.steps() {
            if step.action == Action::Compute {
                let value = compute(graph, eval, hooks, step, &slots)?;
                store(&mut slots, step.node, Some(value));
            }
            for &dead in &step.release {
                store(&mut slots, dead, None);
            }
        }
        self.outputs()
            .iter()
            .map(
                |&id| match slots.get_mut(id.index()).and_then(Option::take) {
                    Some(value) => Ok((id, value)),
                    None => Err(broken(format!(
                        "output '{}' has no value at the end",
                        graph.node(id).name
                    ))),
                },
            )
            .collect()
    }

    /// `execute` for a plan where a value may be absent (`Plan::needs_rules`).
    ///
    /// Each slot carries whether its value is absent. A consumer reading an absent value
    /// gets it by an `Admits` edge, has the argument omitted by an `Optional` one, and is
    /// skipped by a `Required` one: it fires `skipped` and no node event, and its slot holds
    /// the absent value that caused the skip, so an absent output is returned as that value.
    #[inline(never)]
    fn execute_by_rules<P, V: Evaluate<P>>(
        &self,
        graph: &Graph<P>,
        eval: &mut V,
        seeded: Vec<Option<V::Value>>,
        hooks: &mut Hooks<'_, '_, V::Value, V::Error>,
    ) -> Result<Outputs<V::Value>, RunError<V::Error>> {
        let mut slots: Vec<Option<Slot<V::Value>>> = seeded
            .into_iter()
            .map(|value| {
                value.map(|value| Slot {
                    value,
                    absent: false,
                })
            })
            .collect();
        for step in self.steps() {
            let node = graph.node(step.node);
            match step.action {
                Action::UseOverride if node.may_be_absent => {
                    if let Some(slot) = slots.get_mut(step.node.index()).and_then(Option::as_mut) {
                        slot.absent = eval.is_absent(&slot.value);
                    }
                }
                Action::Compute => {
                    let Some(payload) = node.payload() else {
                        return Err(planned_to_compute(node));
                    };
                    let slot = match gather_by_rules(graph, step, &slots)? {
                        Gathered::Args(args) => {
                            let value = call(eval, hooks, node, payload, &args)?;
                            let absent = node.declines && eval.is_absent(&value);
                            Slot { value, absent }
                        }
                        Gathered::Skip(value) => {
                            hooks.check("skipped", |observer| observer.skipped(&node.name, &[]))?;
                            Slot {
                                value,
                                absent: true,
                            }
                        }
                    };
                    store(&mut slots, step.node, Some(slot));
                }
                _ => {}
            }
            for &dead in &step.release {
                store(&mut slots, dead, None);
            }
        }
        self.outputs()
            .iter()
            .map(
                |&id| match slots.get_mut(id.index()).and_then(Option::take) {
                    Some(slot) => Ok((id, slot.value)),
                    None => Err(broken(format!(
                        "output '{}' has no value at the end",
                        graph.node(id).name
                    ))),
                },
            )
            .collect()
    }
}

/// A slot of the absence-aware loop: a value, and whether it is absent.
pub(crate) struct Slot<V> {
    pub(crate) value: V,
    pub(crate) absent: bool,
}

/// A step's arguments under the absence rules, or the absent value that skips it.
enum Gathered<'g, V> {
    /// Every argument the node receives, sorted by input id.
    Args(Vec<Arg<'g, V>>),
    /// A `Required` argument is absent: the node is skipped, and holds this value.
    Skip(V),
}

/// The arguments of a step by the absence rules, values cloned from the slot table.
fn gather_by_rules<'g, P, T: Clone, E>(
    graph: &'g Graph<P>,
    step: &Step,
    slots: &[Option<Slot<T>>],
) -> Result<Gathered<'g, T>, RunError<E>> {
    let consumer = graph.node(step.node);
    // The consumer's edges sorted by input id, made on its first absent argument.
    let mut edges: Option<Vec<(NodeId, Dependency)>> = None;
    let mut args = Vec::with_capacity(step.args.len());
    for &id in &step.args {
        let name = graph.node(id).name.as_str();
        let Some(slot) = slots.get(id.index()).and_then(Option::as_ref) else {
            // A planned argument is always produced earlier and not yet released.
            return Err(broken(format!(
                "argument '{name}' of node '{}' has no value",
                consumer.name
            )));
        };
        if slot.absent {
            let sorted = edges.get_or_insert_with(|| {
                let mut sorted: Vec<_> = consumer
                    .inputs
                    .iter()
                    .map(|edge| (edge.node, edge.dependency))
                    .collect();
                sorted.sort_unstable_by_key(|&(node, _)| node);
                sorted
            });
            let dependency = sorted
                .binary_search_by_key(&id, |&(node, _)| node)
                .ok()
                .and_then(|found| sorted.get(found))
                .map_or(Dependency::Required, |&(_, dependency)| dependency);
            match dependency {
                Dependency::Admits { .. } => {}
                Dependency::Optional => continue,
                Dependency::Required => return Ok(Gathered::Skip(slot.value.clone())),
            }
        }
        args.push(Arg {
            node: id,
            name,
            value: slot.value.clone(),
        });
    }
    Ok(Gathered::Args(args))
}

/// The `RunError::BrokenPlan` for an input node a plan computes.
#[cold]
pub(crate) fn planned_to_compute<P, E>(node: &Node<P>) -> RunError<E> {
    broken(format!("input node '{}' planned to compute", node.name))
}

/// A `RunError::BrokenPlan` with no observer errors yet; the closing hook adds any.
pub(crate) const fn broken<E>(detail: String) -> RunError<E> {
    RunError::BrokenPlan {
        detail,
        observer_errors: Vec::new(),
    }
}

fn store<T>(slots: &mut [Option<T>], id: NodeId, value: Option<T>) {
    if let Some(slot) = slots.get_mut(id.index()) {
        *slot = value;
    }
}

/// Evaluates one `Compute` step between its `pre_node` and `post_node` events.
#[expect(
    clippy::inline_always,
    reason = "the instruction-count gate measures the plain loop, which must not gain a call"
)]
#[inline(always)]
fn compute<P, V: Evaluate<P>>(
    graph: &Graph<P>,
    eval: &mut V,
    hooks: &mut Hooks<'_, '_, V::Value, V::Error>,
    step: &Step,
    slots: &[Option<V::Value>],
) -> Result<V::Value, RunError<V::Error>> {
    let node = graph.node(step.node);
    let Some(payload) = node.payload() else {
        return Err(broken(format!(
            "input node '{}' planned to compute",
            node.name
        )));
    };
    let args = gather(graph, step, slots)?;
    call(eval, hooks, node, payload, &args)
}

/// Evaluates `node` on `args` between its `pre_node` and `post_node` events.
///
/// Always inlined, so the plain loop's `compute` costs what it did before the absence-aware
/// loop shared this with it.
#[expect(
    clippy::inline_always,
    reason = "the instruction-count gate measures the plain loop, which must not gain a call"
)]
#[inline(always)]
fn call<P, V: Evaluate<P>>(
    eval: &mut V,
    hooks: &mut Hooks<'_, '_, V::Value, V::Error>,
    node: &Node<P>,
    payload: &P,
    args: &[Arg<'_, V::Value>],
) -> Result<V::Value, RunError<V::Error>> {
    if hooks.observers.is_empty() {
        // No event to deliver, so the node's arguments and outcome need not be lent out.
        return eval
            .evaluate(node, payload, args)
            .map_err(|source| RunError::Node {
                node: node.name.clone(),
                source,
                observer_errors: Vec::new(),
                branch: Vec::new(),
                block: None,
            });
    }
    hooks.check("pre_node", |observer| observer.pre_node(&node.name, args))?;
    match eval.evaluate(node, payload, args) {
        Ok(value) => hooks
            .check("post_node", |observer| {
                observer.post_node(&node.name, Ok(&value))
            })
            .map(|()| value),
        Err(source) => {
            let observer_errors =
                hooks.fire(|observer| observer.post_node(&node.name, Err(&source)));
            Err(RunError::Node {
                node: node.name.clone(),
                source,
                observer_errors,
                branch: Vec::new(),
                block: None,
            })
        }
    }
}

/// The arguments of a step, values cloned from the slot table.
fn gather<'g, P, T: Clone, E>(
    graph: &'g Graph<P>,
    step: &Step,
    slots: &[Option<T>],
) -> Result<Vec<Arg<'g, T>>, RunError<E>> {
    step.args
        .iter()
        .map(|&id| {
            let name = graph.node(id).name.as_str();
            match slots.get(id.index()).and_then(Option::as_ref) {
                Some(value) => Ok(Arg {
                    node: id,
                    name,
                    value: value.clone(),
                }),
                // A planned argument is always produced earlier and not yet released.
                None => Err(broken(format!(
                    "argument '{name}' of node '{}' has no value",
                    graph.node(step.node).name
                ))),
            }
        })
        .collect()
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    use super::*;

    use crate::plan::Action;
    use crate::testing::{declining, node, request, valued, valued_spec};

    fn map<V: Clone>(pairs: &[(&str, V)]) -> BTreeMap<String, V> {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), v.clone()))
            .collect()
    }

    /// a <- x, b <- a, c <- a, d <- b, c; payloads 1, 10, 100, 1000.
    fn diamond() -> Graph<i64> {
        Graph::build(vec![
            valued("a", &["x"], 1),
            valued("b", &["a"], 10),
            valued("c", &["a"], 100),
            valued("d", &["b", "c"], 1000),
        ])
        .unwrap()
    }

    /// Value = payload + sum of args; records every call and can fail one node.
    #[derive(Default)]
    struct Adder {
        calls: Vec<(String, Vec<(String, i64)>)>,
        fail_on: Option<&'static str>,
    }

    impl Evaluate<i64> for Adder {
        type Value = i64;
        type Error = String;

        fn evaluate(
            &mut self,
            node: &Node<i64>,
            payload: &i64,
            args: &[Arg<'_, i64>],
        ) -> Result<i64, String> {
            self.calls.push((
                node.name.clone(),
                args.iter().map(|a| (a.name.to_owned(), a.value)).collect(),
            ));
            if self.fail_on == Some(node.name.as_str()) {
                return Err(format!("{} failed", node.name));
            }
            Ok(payload + args.iter().map(|a| a.value).sum::<i64>())
        }
    }

    type Log = Rc<RefCell<Vec<String>>>;

    /// Logs `<name>:<event>`; fails with `<name>:<event>` when the event is in `fail_on`.
    struct Recorder {
        name: &'static str,
        log: Log,
        fail_on: Vec<&'static str>,
    }

    impl Recorder {
        fn new(name: &'static str, log: &Log, fail_on: &[&'static str]) -> Self {
            Self {
                name,
                log: Rc::clone(log),
                fail_on: fail_on.to_vec(),
            }
        }

        fn event(&mut self, event: &str) -> Result<(), String> {
            let entry = format!("{}:{event}", self.name);
            self.log.borrow_mut().push(entry.clone());
            if self.fail_on.contains(&event) {
                Err(entry)
            } else {
                Ok(())
            }
        }
    }

    impl Observer<i64, String> for Recorder {
        fn pre_graph(&mut self) -> Result<(), String> {
            self.event("pre_graph")
        }
        fn pre_node(&mut self, node: &str, _args: &[Arg<'_, i64>]) -> Result<(), String> {
            self.event(&format!("pre_node:{node}"))
        }
        fn post_node(&mut self, node: &str, outcome: Result<&i64, &String>) -> Result<(), String> {
            self.event(&format!("post_node:{node}:{}", outcome.is_ok()))
        }
        fn post_graph(&mut self, ok: bool) -> Result<(), String> {
            self.event(&format!("post_graph:{ok}"))
        }
        fn skipped(&mut self, node: &str, branch: &[usize]) -> Result<(), String> {
            self.event(&format!("skipped:{node}:{branch:?}"))
        }
    }

    fn run_with(
        graph: &Graph<i64>,
        plan: &Plan,
        eval: &mut Adder,
        observers: &mut [&mut dyn Observer<i64, String>],
    ) -> Result<BTreeMap<String, i64>, RunError<String>> {
        plan.run(graph, eval, &map(&[("x", 5)]), &BTreeMap::new(), observers)
    }

    fn log_of(log: &Log) -> Vec<String> {
        log.borrow().clone()
    }

    #[test]
    fn runs_a_diamond_and_returns_only_requested_outputs() {
        let g = diamond();
        let plan = g.prepare(&request(&["d", "b"], &["x"], &[])).unwrap();
        let out = run_with(&g, &plan, &mut Adder::default(), &mut []).unwrap();
        // a = 1 + 5, b = 10 + 6, c = 100 + 6, d = 1000 + 16 + 106.
        assert_eq!(out, map(&[("b", 16), ("d", 1122)]));
    }

    #[test]
    fn run_by_id_reads_values_by_node_and_returns_outputs_by_id() {
        let g = diamond();
        let plan = g.prepare(&request(&["d", "b"], &["x"], &[])).unwrap();
        let x = g.id_of("x").unwrap();
        let mut asked = Vec::new();
        let out = plan
            .run_by_id(
                &g,
                &mut Adder::default(),
                |action, id| {
                    asked.push((action, id));
                    (id == x).then_some(5)
                },
                &mut [],
            )
            .unwrap();
        assert_eq!(asked, vec![(Action::UseInput, x)]);
        let named: Vec<_> = out
            .iter()
            .map(|&(id, v)| (g.node(id).name.as_str(), v))
            .collect();
        assert_eq!(named, vec![("b", 16), ("d", 1122)]);
    }

    #[test]
    fn run_by_id_reports_a_value_lookup_does_not_have() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &["x"], &[])).unwrap();
        let err = plan
            .run_by_id(&g, &mut Adder::default(), |_, _| None, &mut [])
            .unwrap_err();
        assert!(matches!(err, RunError::MissingValues(names) if names == ["x"]));
    }

    /// Option<i64> values: `None` is a legitimate supplied value.
    struct Presence;

    impl Evaluate<()> for Presence {
        type Value = Option<i64>;
        type Error = String;

        fn evaluate(
            &mut self,
            _node: &Node<()>,
            _payload: &(),
            args: &[Arg<'_, Option<i64>>],
        ) -> Result<Option<i64>, String> {
            Ok(Some(i64::try_from(args.len()).unwrap()))
        }
    }

    #[test]
    fn a_none_valued_input_counts_as_supplied() {
        let g = Graph::build(vec![node("out", &["x"])]).unwrap();
        let plan = g.prepare(&request(&["out"], &["x"], &[])).unwrap();
        let out = plan
            .run(
                &g,
                &mut Presence,
                &map(&[("x", None)]),
                &BTreeMap::new(),
                &mut [],
            )
            .unwrap();
        assert_eq!(out, map(&[("out", Some(1))]));
    }

    #[test]
    fn missing_values_are_reported_sorted_before_anything_runs() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &["x"], &["b"])).unwrap();
        let log = Log::default();
        let mut recorder = Recorder::new("r", &log, &[]);
        let mut eval = Adder::default();
        let err = plan
            .run(
                &g,
                &mut eval,
                &BTreeMap::new(),
                &map(&[("zzz", 1)]),
                &mut [&mut recorder],
            )
            .unwrap_err();
        assert_eq!(
            err,
            RunError::MissingValues(vec!["b".to_owned(), "x".to_owned()])
        );
        assert!(log_of(&log).is_empty() && eval.calls.is_empty());
    }

    #[test]
    fn extra_keys_in_the_maps_are_ignored() {
        let g = diamond();
        let plan = g.prepare(&request(&["a"], &["x"], &[])).unwrap();
        let out = plan
            .run(
                &g,
                &mut Adder::default(),
                &map(&[("x", 5), ("junk", 9)]),
                &map(&[("d", 7)]),
                &mut [],
            )
            .unwrap();
        assert_eq!(out, map(&[("a", 6)]));
    }

    /// Counts live instances, so a test can see when the executor drops a value.
    struct Counted {
        value: i64,
        live: Rc<Cell<usize>>,
    }

    impl Counted {
        fn new(value: i64, live: &Rc<Cell<usize>>) -> Self {
            live.set(live.get() + 1);
            Self {
                value,
                live: Rc::clone(live),
            }
        }
    }

    impl Clone for Counted {
        fn clone(&self) -> Self {
            Self::new(self.value, &self.live)
        }
    }

    impl Drop for Counted {
        fn drop(&mut self) {
            self.live.set(self.live.get() - 1);
        }
    }

    /// Records how many `Counted` values are alive when each node starts evaluating.
    struct CountingEval {
        live: Rc<Cell<usize>>,
        seen: Vec<(String, usize)>,
        received: Vec<(String, i64)>,
    }

    impl CountingEval {
        fn new(live: &Rc<Cell<usize>>) -> Self {
            Self {
                live: Rc::clone(live),
                seen: Vec::new(),
                received: Vec::new(),
            }
        }
    }

    impl Evaluate<i64> for CountingEval {
        type Value = Counted;
        type Error = String;

        fn evaluate(
            &mut self,
            node: &Node<i64>,
            payload: &i64,
            args: &[Arg<'_, Counted>],
        ) -> Result<Counted, String> {
            self.seen.push((node.name.clone(), self.live.get()));
            self.received.extend(
                args.iter()
                    .map(|a| (format!("{}<-{}", node.name, a.name), a.value.value)),
            );
            let sum = args.iter().map(|a| a.value.value).sum::<i64>();
            Ok(Counted::new(payload + sum, &self.live))
        }
    }

    #[test]
    fn released_intermediates_are_dropped() {
        let live = Rc::new(Cell::new(0));
        let g = Graph::build(vec![
            valued("a", &["x"], 1),
            valued("b", &["a"], 1),
            valued("c", &["b"], 1),
        ])
        .unwrap();
        let plan = g.prepare(&request(&["c"], &["x"], &[])).unwrap();
        let mut eval = CountingEval::new(&live);
        let inputs = map(&[("x", Counted::new(5, &live))]);
        let out = plan
            .run(&g, &mut eval, &inputs, &BTreeMap::new(), &mut [])
            .unwrap();
        // Alive at each start: the caller's map entry, the node's slot input, its args clone.
        // Were `x` or `a` kept past their last use, `b` and `c` would see one more.
        assert_eq!(
            eval.seen,
            [
                ("a".to_owned(), 3),
                ("b".to_owned(), 3),
                ("c".to_owned(), 3)
            ]
        );
        // The caller's map entry and the one output remain.
        assert_eq!(live.get(), 2);
        drop(out);
        assert_eq!(live.get(), 1);
    }

    #[test]
    fn an_override_cuts_one_branch_but_the_other_still_runs() {
        let live = Rc::new(Cell::new(0));
        let g = Graph::build(vec![
            valued("a", &["x"], 1),
            valued("b", &["a"], 10),
            valued("c", &["a"], 100),
            valued("d", &["b", "c"], 1000),
        ])
        .unwrap();
        let plan = g.prepare(&request(&["d"], &["x"], &["b"])).unwrap();
        let mut eval = CountingEval::new(&live);
        let inputs = map(&[("x", Counted::new(5, &live))]);
        let overrides = map(&[("b", Counted::new(7, &live))]);
        let out = plan
            .run(&g, &mut eval, &inputs, &overrides, &mut [])
            .unwrap();
        let computed: Vec<&str> = eval.seen.iter().map(|(n, _)| n.as_str()).collect();
        assert_eq!(computed, ["a", "c", "d"]);
        // `d` receives the override (7), not what `b` would have computed (16).
        assert!(eval.received.contains(&("d<-b".to_owned(), 7)));
        assert_eq!(out["d"].value, 1000 + 7 + 106);
        // Caller's x and b, plus the output d; the override's slot copy is gone.
        assert_eq!(live.get(), 3);
        // At `d`: caller's x and b, the override slot, c's slot, and args b and c.
        assert_eq!(eval.seen.last().unwrap().1, 6);
    }

    #[test]
    fn a_node_error_returns_node_name_and_still_calls_post_graph_false() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &["x"], &[])).unwrap();
        let log = Log::default();
        let mut recorder = Recorder::new("r", &log, &[]);
        let mut eval = Adder {
            fail_on: Some("b"),
            ..Adder::default()
        };
        let err = run_with(&g, &plan, &mut eval, &mut [&mut recorder]).unwrap_err();
        assert_eq!(
            err,
            RunError::Node {
                node: "b".to_owned(),
                source: "b failed".to_owned(),
                observer_errors: Vec::new(),
                branch: Vec::new(),
                block: None,
            }
        );
        assert_eq!(
            log_of(&log),
            [
                "r:pre_graph",
                "r:pre_node:a",
                "r:post_node:a:true",
                "r:pre_node:b",
                "r:post_node:b:false",
                "r:post_graph:false",
            ]
        );
        assert_eq!(eval.calls.len(), 2);
    }

    #[test]
    fn observer_errors_during_a_node_failure_are_kept() {
        let g = diamond();
        let plan = g.prepare(&request(&["b"], &["x"], &[])).unwrap();
        let log = Log::default();
        let mut first = Recorder::new("p", &log, &["post_node:b:false", "post_graph:false"]);
        let mut second = Recorder::new("q", &log, &["post_graph:false"]);
        let mut eval = Adder {
            fail_on: Some("b"),
            ..Adder::default()
        };
        let err = run_with(&g, &plan, &mut eval, &mut [&mut first, &mut second]).unwrap_err();
        let RunError::Node {
            node,
            source,
            observer_errors,
            ..
        } = err
        else {
            unreachable!("expected a node error");
        };
        assert_eq!((node.as_str(), source.as_str()), ("b", "b failed"));
        assert_eq!(
            observer_errors,
            [
                "p:post_node:b:false",
                "p:post_graph:false",
                "q:post_graph:false"
            ]
        );
    }

    #[test]
    fn every_observer_runs_for_an_event_before_errors_are_returned() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &["x"], &[])).unwrap();
        let log = Log::default();
        let mut first = Recorder::new("p", &log, &["post_node:a:true"]);
        let mut second = Recorder::new("q", &log, &[]);
        let mut eval = Adder::default();
        let err = run_with(&g, &plan, &mut eval, &mut [&mut first, &mut second]).unwrap_err();
        assert_eq!(
            err,
            RunError::Observers {
                event: "post_node",
                errors: vec!["p:post_node:a:true".to_owned()],
            }
        );
        assert_eq!(
            log_of(&log),
            [
                "p:pre_graph",
                "q:pre_graph",
                "p:pre_node:a",
                "q:pre_node:a",
                "p:post_node:a:true",
                "q:post_node:a:true",
                "p:post_graph:false",
                "q:post_graph:false",
            ]
        );
        assert_eq!(eval.calls.len(), 1);
    }

    /// Logs each node's arguments before it runs and its outcome after.
    #[derive(Default)]
    struct ValueLog(Vec<String>);

    impl Observer<i64, String> for ValueLog {
        fn pre_node(&mut self, node: &str, args: &[Arg<'_, i64>]) -> Result<(), String> {
            let args: Vec<_> = args
                .iter()
                .map(|a| format!("{}={}", a.name, a.value))
                .collect();
            self.0.push(format!("{node}({})", args.join(", ")));
            Ok(())
        }
        fn post_node(&mut self, node: &str, outcome: Result<&i64, &String>) -> Result<(), String> {
            self.0.push(format!("{node} -> {outcome:?}"));
            Ok(())
        }
    }

    #[test]
    fn observers_see_each_nodes_arguments_and_result() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &["x"], &[])).unwrap();
        let mut log = ValueLog::default();
        let out = run_with(&g, &plan, &mut Adder::default(), &mut [&mut log]).unwrap();
        assert_eq!(out, map(&[("d", 1122)]));
        assert_eq!(
            log.0,
            [
                "a(x=5)",
                "a -> Ok(6)",
                "b(a=6)",
                "b -> Ok(16)",
                "c(a=6)",
                "c -> Ok(106)",
                "d(b=16, c=106)",
                "d -> Ok(1122)",
            ]
        );
    }

    #[test]
    fn observers_see_the_error_a_node_returned() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &["x"], &[])).unwrap();
        let mut log = ValueLog::default();
        let mut eval = Adder {
            fail_on: Some("b"),
            ..Adder::default()
        };
        let err = run_with(&g, &plan, &mut eval, &mut [&mut log]).unwrap_err();
        assert!(matches!(err, RunError::Node { node, .. } if node == "b"));
        assert_eq!(
            log.0,
            ["a(x=5)", "a -> Ok(6)", "b(a=6)", "b -> Err(\"b failed\")"]
        );
    }

    #[test]
    fn a_failing_pre_graph_skips_every_node_and_appends_post_graph_errors() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &["x"], &[])).unwrap();
        let log = Log::default();
        let mut first = Recorder::new("p", &log, &["pre_graph", "post_graph:false"]);
        let mut second = Recorder::new("q", &log, &["pre_graph"]);
        let mut eval = Adder::default();
        let err = run_with(&g, &plan, &mut eval, &mut [&mut first, &mut second]).unwrap_err();
        assert_eq!(
            err,
            RunError::Observers {
                event: "pre_graph",
                errors: vec![
                    "p:pre_graph".to_owned(),
                    "q:pre_graph".to_owned(),
                    "p:post_graph:false".to_owned()
                ],
            }
        );
        assert_eq!(eval.calls.len(), 0);
    }

    #[test]
    fn a_failing_pre_node_skips_the_node_and_its_post_node() {
        let g = diamond();
        let plan = g.prepare(&request(&["a"], &["x"], &[])).unwrap();
        let log = Log::default();
        let mut recorder = Recorder::new("r", &log, &["pre_node:a"]);
        let mut eval = Adder::default();
        let err = run_with(&g, &plan, &mut eval, &mut [&mut recorder]).unwrap_err();
        assert!(matches!(
            err,
            RunError::Observers {
                event: "pre_node",
                ..
            }
        ));
        assert_eq!(
            log_of(&log),
            ["r:pre_graph", "r:pre_node:a", "r:post_graph:false"]
        );
        assert_eq!(eval.calls.len(), 0);
    }

    #[test]
    fn post_graph_true_errors_become_observer_errors_and_node_hooks_skip_non_compute_steps() {
        let g = diamond();
        let plan = g.prepare(&request(&["a"], &["x"], &[])).unwrap();
        let log = Log::default();
        let mut recorder = Recorder::new("r", &log, &["post_graph:true"]);
        let err = run_with(&g, &plan, &mut Adder::default(), &mut [&mut recorder]).unwrap_err();
        assert_eq!(
            err,
            RunError::Observers {
                event: "post_graph",
                errors: vec!["r:post_graph:true".to_owned()]
            }
        );
        // `x` is a UseInput step: no node hooks for it.
        assert_eq!(
            log_of(&log),
            [
                "r:pre_graph",
                "r:pre_node:a",
                "r:post_node:a:true",
                "r:post_graph:true"
            ]
        );
    }

    #[test]
    fn a_broken_plan_keeps_the_closing_observer_errors_and_quotes_names() {
        let failure: RunError<String> = broken("argument 'a' of node 'b' has no value".to_owned());
        let failure = failure.with_closing(vec!["p:post_graph:false".to_owned()]);
        assert_eq!(
            failure,
            RunError::BrokenPlan {
                detail: "argument 'a' of node 'b' has no value".to_owned(),
                observer_errors: vec!["p:post_graph:false".to_owned()],
            }
        );
        assert_eq!(
            failure.to_string(),
            "broken plan: argument 'a' of node 'b' has no value"
        );
        assert_eq!(
            RunError::<String>::MissingValues(vec!["a".to_owned(), "b".to_owned()]).to_string(),
            "missing values: 'a', 'b'"
        );
    }

    #[test]
    fn a_plan_that_computes_an_input_of_another_graph_is_a_broken_plan() {
        let planned = Graph::build(vec![valued("a", &["x"], 1)]).unwrap();
        let plan = planned.prepare(&request(&["a"], &["x"], &[])).unwrap();
        // The same ids in other roles: `a` is an input here, and `b` has the id `x` had.
        let other = Graph::build(vec![valued("b", &["a"], 1)]).unwrap();
        let err = plan
            .run(
                &other,
                &mut Adder::default(),
                &map(&[("b", 5)]),
                &BTreeMap::new(),
                &mut [],
            )
            .unwrap_err();
        assert_eq!(err, broken("input node 'a' planned to compute".to_owned()));
    }

    #[test]
    fn a_chain_of_ten_thousand_nodes_runs_without_stack_overflow() {
        let last = 10_000;
        let specs: Vec<_> = (0..last)
            .map(|i| valued(&format!("n{i:05}"), &[&format!("n{:05}", i + 1)], 1))
            .collect();
        let g = Graph::build(specs).unwrap();
        let tail = format!("n{last:05}");
        let plan = g.prepare(&request(&["n00000"], &[&tail], &[])).unwrap();
        let out = plan
            .run(
                &g,
                &mut Adder::default(),
                &map(&[(tail.as_str(), 0)]),
                &BTreeMap::new(),
                &mut [],
            )
            .unwrap();
        assert_eq!(out["n00000"], i64::from(last));
    }

    mod absence {
        //! Nodes that may decline, and the three rules their consumers follow.

        use super::*;

        /// The value a declining node returns, and that `Marker::is_absent` recognises.
        const ABSENT: i64 = -1;

        /// Value = payload + sum of args, except that a payload of `ABSENT` declines.
        /// Records every call with its arguments, and counts `is_absent` questions.
        #[derive(Default)]
        struct Marker {
            calls: Vec<(String, Vec<(String, i64)>)>,
            asked: Cell<usize>,
        }

        impl Evaluate<i64> for Marker {
            type Value = i64;
            type Error = String;

            fn evaluate(
                &mut self,
                node: &Node<i64>,
                payload: &i64,
                args: &[Arg<'_, i64>],
            ) -> Result<i64, String> {
                self.calls.push((
                    node.name.clone(),
                    args.iter().map(|a| (a.name.to_owned(), a.value)).collect(),
                ));
                if *payload == ABSENT {
                    return Ok(ABSENT);
                }
                Ok(payload + args.iter().map(|a| a.value).sum::<i64>())
            }

            fn is_absent(&self, value: &i64) -> bool {
                self.asked.set(self.asked.get() + 1);
                *value == ABSENT
            }
        }

        impl Marker {
            fn called(&self) -> Vec<&str> {
                self.calls.iter().map(|(name, _)| name.as_str()).collect()
            }
        }

        /// `a` declines; `req` requires it, `opt` has a default for it, `adm` admits it.
        fn consumers() -> Graph<i64> {
            Graph::build(vec![
                declining("a", &[], ABSENT),
                valued_spec("req", &[("a", Dependency::Required)], 10),
                valued_spec(
                    "opt",
                    &[("a", Dependency::Optional), ("x", Dependency::Required)],
                    20,
                ),
                valued_spec("adm", &[("a", Dependency::Admits { optional: false })], 30),
            ])
            .unwrap()
        }

        fn run_marker(
            graph: &Graph<i64>,
            outputs: &[&str],
            overrides: &[(&str, i64)],
            eval: &mut Marker,
        ) -> Result<BTreeMap<String, i64>, RunError<String>> {
            let names: Vec<&str> = overrides.iter().map(|(n, _)| *n).collect();
            let plan = graph.prepare(&request(outputs, &["x"], &names)).unwrap();
            plan.run(graph, eval, &map(&[("x", 5)]), &map(overrides), &mut [])
        }

        /// A run's outcome, its evaluator and its observer log.
        type Logged = (
            Result<BTreeMap<String, i64>, RunError<String>>,
            Marker,
            Vec<String>,
        );

        fn run_logged(graph: &Graph<i64>, output: &str, fail_on: &[&'static str]) -> Logged {
            let plan = graph.prepare(&request(&[output], &[], &[])).unwrap();
            let log = Log::default();
            let mut recorder = Recorder::new("r", &log, fail_on);
            let mut eval = Marker::default();
            let out = plan.run(
                graph,
                &mut eval,
                &BTreeMap::new(),
                &BTreeMap::new(),
                &mut [&mut recorder],
            );
            (out, eval, log_of(&log))
        }

        #[test]
        fn a_declined_value_skips_required_consumers_and_reaches_admitting_ones() {
            let g = consumers();
            let mut eval = Marker::default();
            let out = run_marker(&g, &["req", "adm"], &[], &mut eval).unwrap();
            assert_eq!(eval.called(), ["a", "adm"]);
            assert_eq!(eval.calls[1].1, [("a".to_owned(), ABSENT)]);
            // The skipped node holds the absent value that caused the skip.
            assert_eq!(out, map(&[("adm", 30 + ABSENT), ("req", ABSENT)]));
        }

        #[test]
        fn an_optional_edge_from_an_absent_node_is_omitted() {
            let g = consumers();
            let mut eval = Marker::default();
            let out = run_marker(&g, &["opt"], &[], &mut eval).unwrap();
            assert_eq!(eval.called(), ["a", "opt"]);
            assert_eq!(eval.calls[1].1, [("x".to_owned(), 5)]);
            assert_eq!(out, map(&[("opt", 25)]));
        }

        #[test]
        fn a_present_value_from_a_declining_node_reaches_every_consumer() {
            let g = Graph::build(vec![
                declining("a", &[], 7),
                valued_spec("req", &[("a", Dependency::Required)], 10),
                valued_spec("opt", &[("a", Dependency::Optional)], 20),
            ])
            .unwrap();
            let plan = g.prepare(&request(&["req", "opt"], &[], &[])).unwrap();
            let mut eval = Marker::default();
            let out = plan
                .run(&g, &mut eval, &BTreeMap::new(), &BTreeMap::new(), &mut [])
                .unwrap();
            assert_eq!(out, map(&[("opt", 27), ("req", 17)]));
        }

        #[test]
        fn a_skipped_node_fires_skipped_and_no_node_events() {
            let g = Graph::build(vec![
                declining("a", &[], ABSENT),
                valued_spec("b", &[("a", Dependency::Required)], 10),
            ])
            .unwrap();
            let (out, _, log) = run_logged(&g, "b", &[]);
            assert_eq!(out.unwrap(), map(&[("b", ABSENT)]));
            assert_eq!(
                log,
                [
                    "r:pre_graph",
                    "r:pre_node:a",
                    "r:post_node:a:true",
                    "r:skipped:b:[]",
                    "r:post_graph:true",
                ]
            );
        }

        #[test]
        fn a_failing_skipped_event_aborts_the_run() {
            let g = Graph::build(vec![
                declining("a", &[], ABSENT),
                valued_spec("b", &[("a", Dependency::Required)], 10),
                valued_spec("c", &[("b", Dependency::Required)], 10),
            ])
            .unwrap();
            let (out, _, log) = run_logged(&g, "c", &["skipped:b:[]"]);
            assert_eq!(
                out.unwrap_err(),
                RunError::Observers {
                    event: "skipped",
                    errors: vec!["r:skipped:b:[]".to_owned()],
                }
            );
            assert_eq!(log.last().unwrap(), "r:post_graph:false");
            assert!(!log.iter().any(|e| e.contains(":c")));
        }

        #[test]
        fn an_absent_output_is_returned_with_its_value() {
            let g = consumers();
            let out = run_marker(&g, &["a"], &[], &mut Marker::default()).unwrap();
            assert_eq!(out, map(&[("a", ABSENT)]));
        }

        #[test]
        fn an_absent_override_skips_consumers() {
            let g = consumers();
            let mut eval = Marker::default();
            let out = run_marker(&g, &["req", "adm"], &[("a", ABSENT)], &mut eval).unwrap();
            assert_eq!(eval.called(), ["adm"]);
            assert_eq!(out, map(&[("adm", 30 + ABSENT), ("req", ABSENT)]));
            // A present override reaches every consumer.
            let out = run_marker(&g, &["req"], &[("a", 1)], &mut Marker::default()).unwrap();
            assert_eq!(out, map(&[("req", 11)]));
        }

        #[test]
        fn is_absent_is_asked_only_of_declining_nodes() {
            let mut specs: Vec<_> = (0..200)
                .map(|i| valued(&format!("n{i:03}"), &[&format!("n{:03}", i + 1)], 1))
                .collect();
            specs.push(valued("n200", &[], 0));
            let g = Graph::build(specs).unwrap();
            let plan = g.prepare(&request(&["n000"], &[], &[])).unwrap();
            let mut eval = Marker::default();
            plan.run(&g, &mut eval, &BTreeMap::new(), &BTreeMap::new(), &mut [])
                .unwrap();
            assert_eq!(eval.calls.len(), 201);
            assert_eq!(eval.asked.get(), 0);

            // With one declining node, only it is asked, not the consumers it reaches.
            let g = Graph::build(vec![
                declining("a", &[], 3),
                valued_spec("b", &[("a", Dependency::Required)], 1),
                valued_spec("c", &[("b", Dependency::Required)], 1),
            ])
            .unwrap();
            let (out, eval, _) = run_logged(&g, "c", &[]);
            assert_eq!(out.unwrap(), map(&[("c", 5)]));
            assert_eq!(eval.asked.get(), 1);
        }

        #[test]
        fn a_ten_thousand_node_skip_chain_runs_without_recursion() {
            let last = 10_000;
            let mut specs: Vec<_> = (0..last)
                .map(|i| valued(&format!("n{i:05}"), &[&format!("n{:05}", i + 1)], 1))
                .collect();
            specs.push(declining(&format!("n{last:05}"), &[], ABSENT));
            let g = Graph::build(specs).unwrap();
            let (out, eval, log) = run_logged(&g, "n00000", &[]);
            assert_eq!(out.unwrap(), map(&[("n00000", ABSENT)]));
            assert_eq!(eval.called().len(), 1);
            let skipped = log.iter().filter(|e| e.starts_with("r:skipped:")).count();
            assert_eq!(skipped, 10_000);
        }

        #[test]
        fn an_admits_edge_to_an_unsupplied_input_is_missing_unless_defaulted() {
            let g = Graph::build(vec![
                valued_spec("need", &[("i", Dependency::Admits { optional: false })], 1),
                valued_spec("opt", &[("i", Dependency::Admits { optional: true })], 1),
            ])
            .unwrap();
            let err = g.prepare(&request(&["need"], &[], &[])).unwrap_err();
            assert_eq!(err.to_string(), "missing inputs: 'i' (needed by 'need')");
            let plan = g.prepare(&request(&["opt"], &[], &[])).unwrap();
            let actions: Vec<Action> = plan.steps().iter().map(|s| s.action).collect();
            assert_eq!(actions, [Action::SkipOptionalInput, Action::Compute]);
            assert_eq!(plan.steps()[1].args, [] as [NodeId; 0]);
            let out = plan
                .run(
                    &g,
                    &mut Marker::default(),
                    &BTreeMap::new(),
                    &BTreeMap::new(),
                    &mut [],
                )
                .unwrap();
            assert_eq!(out, map(&[("opt", 1)]));
        }

        #[test]
        fn a_plan_with_nothing_absent_runs_the_plain_loop() {
            let plan = diamond().prepare(&request(&["d"], &["x"], &[])).unwrap();
            assert!(!plan.needs_rules());
            let g = consumers();
            // `opt` reads the declining `a`, which is planned.
            let plan = g.prepare(&request(&["opt"], &["x"], &[])).unwrap();
            assert!(plan.needs_rules());
            // An override of `opt` cuts `a` away, and `opt` itself is never absent.
            let plan = g.prepare(&request(&["opt"], &[], &["opt"])).unwrap();
            assert!(!plan.needs_rules());
            // An override of `a` itself may be absent.
            let plan = g.prepare(&request(&["req"], &[], &["a"])).unwrap();
            assert!(plan.needs_rules());
        }
    }
}
