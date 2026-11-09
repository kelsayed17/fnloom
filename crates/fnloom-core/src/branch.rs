//! Running a plan with expands: each block once per item, in reference order.
//!
//! The run keeps one slot table, as the plain loop does. A branch writes its block's slots,
//! and at its end the values its collects read are moved out into the block's frame, so the
//! next branch starts from empty slots. Frames sit on an explicit stack, one per open block,
//! so nesting never recurses.

use crate::graph::Graph;
use crate::node::{Collect, Dependency, Edge, Node, NodeId};
use crate::plan::{Action, BlockId, Plan, Step, SubPlan};
use crate::run::{Arg, Evaluate, Hooks, Item, Outputs, RunError, Slot, broken, planned_to_compute};

/// What a collect reads of one node: its value in each branch, or the absent value of an
/// expand that had none.
enum Collected<V> {
    Branches(Vec<Slot<V>>),
    Absent(V),
}

/// What a branch does with one step of its sub-plan, decided before it runs.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Need {
    /// Nothing needs it in this branch.
    Skip,
    /// `Evaluate::lookup` gave its value.
    Hit,
    /// It is computed.
    Run,
}

/// One open block: its iterator, the branch it is in, and what its branches collected.
struct Frame<V> {
    block: BlockId,
    iterator: V,
    /// Items pulled so far; the current branch's index is one less.
    index: usize,
    /// The next step of the current branch.
    position: usize,
    need: Vec<Need>,
    /// Per node of `SubPlan::collected`, its slot from each branch so far.
    collected: Vec<Vec<Slot<V>>>,
}

/// A step's arguments, or the absent value that skips it.
enum Gathered<'g, V> {
    Args(Vec<Arg<'g, V>>),
    Skip(V),
}

/// The state of one run of a plan with expands.
struct Runner<'r, P, V> {
    plan: &'r Plan,
    graph: &'r Graph<P>,
    slots: Vec<Option<Slot<V>>>,
    /// Per collected node, what its collects read, once its block has run.
    gathered: Vec<Option<Collected<V>>>,
    frames: Vec<Frame<V>>,
    /// The current branch path, one index per open frame.
    branch: Vec<usize>,
    /// The next step of the outer plan.
    outer: usize,
    /// Scratch for the walk that decides a branch's needs; all clear between walks.
    wanted: Vec<bool>,
}

/// Runs `plan`, which has an expand step, from the seeded slot table.
#[inline(never)]
pub(crate) fn execute<P, V: Evaluate<P>>(
    plan: &Plan,
    graph: &Graph<P>,
    eval: &mut V,
    seeded: Vec<Option<V::Value>>,
    hooks: &mut Hooks<'_, '_, V::Value, V::Error>,
) -> Result<Outputs<V::Value>, RunError<V::Error>> {
    let count = seeded.len();
    let mut runner = Runner {
        plan,
        graph,
        slots: seeded
            .into_iter()
            .map(|value| {
                value.map(|value| Slot {
                    value,
                    absent: false,
                })
            })
            .collect(),
        gathered: (0..count).map(|_| None).collect(),
        frames: Vec::new(),
        branch: Vec::new(),
        outer: 0,
        wanted: vec![false; count],
    };
    if let Err(failure) = runner.drive(eval, hooks) {
        runner.close(eval);
        return Err(failure);
    }
    plan.outputs()
        .iter()
        .map(
            |&id| match runner.slots.get_mut(id.index()).and_then(Option::take) {
                Some(slot) => Ok((id, slot.value)),
                None => Err(broken(format!(
                    "output '{}' has no value at the end",
                    graph.node(id).name
                ))),
            },
        )
        .collect()
}

impl<'r, P, T: Clone> Runner<'r, P, T> {
    /// Runs every step, branch by branch, until the outer plan is done.
    fn drive<V: Evaluate<P, Value = T>>(
        &mut self,
        eval: &mut V,
        hooks: &mut Hooks<'_, '_, T, V::Error>,
    ) -> Result<(), RunError<V::Error>> {
        let plan = self.plan;
        loop {
            let (steps, position, need) = match self.frames.last() {
                Some(frame) => (
                    plan.blocks()[frame.block.index()].steps.as_slice(),
                    frame.position,
                    frame.need.get(frame.position).copied(),
                ),
                None => (plan.steps(), self.outer, Some(Need::Run)),
            };
            let Some(step) = steps.get(position) else {
                if self.frames.is_empty() {
                    return Ok(());
                }
                self.end_branch()?;
                self.pull(eval, hooks)?;
                continue;
            };
            let done = match need.unwrap_or(Need::Skip) {
                Need::Skip => true,
                Need::Hit => self.after_value(step, eval, hooks)?,
                Need::Run => self.run_step(step, eval, hooks)?,
            };
            if done {
                self.complete(step);
            }
        }
    }

    /// Releases `step`'s dead values and moves past it, in the innermost open scope.
    fn complete(&mut self, step: &Step) {
        for &dead in &step.release {
            if let Some(slot) = self.slots.get_mut(dead.index()) {
                *slot = None;
            }
            if let Some(gathered) = self.gathered.get_mut(dead.index()) {
                *gathered = None;
            }
        }
        match self.frames.last_mut() {
            Some(frame) => frame.position += 1,
            None => self.outer += 1,
        }
    }

    /// `complete` for the step the innermost open scope is at: the expand whose block just
    /// ended.
    fn complete_current(&mut self) {
        let plan = self.plan;
        let step = match self.frames.last() {
            Some(frame) => plan.blocks()[frame.block.index()].steps.get(frame.position),
            None => plan.steps().get(self.outer),
        };
        if let Some(step) = step {
            self.complete(step);
        }
    }

    /// Runs one step. `false` when it opened a block, which completes the step once its
    /// items are exhausted.
    fn run_step<V: Evaluate<P, Value = T>>(
        &mut self,
        step: &Step,
        eval: &mut V,
        hooks: &mut Hooks<'_, '_, T, V::Error>,
    ) -> Result<bool, RunError<V::Error>> {
        let graph = self.graph;
        let node = graph.node(step.node);
        match step.action {
            Action::UseOverride => {
                if node.may_be_absent {
                    if let Some(slot) = self
                        .slots
                        .get_mut(step.node.index())
                        .and_then(Option::as_mut)
                    {
                        slot.absent = eval.is_absent(&slot.value);
                    }
                }
                self.after_value(step, eval, hooks)
            }
            Action::Compute => {
                if self.plan.is_late(step.node) && self.frames.is_empty() {
                    match eval.lookup(node, &[]) {
                        Err(source) => {
                            return Err(node_error(graph, node, source, &[], Vec::new()));
                        }
                        Ok(Some(value)) => {
                            hooks.check("hit", |observer| {
                                observer.hit_in(&node.name, &[], &value)
                            })?;
                            let absent = node.may_be_absent && eval.is_absent(&value);
                            self.store(step.node, Slot { value, absent });
                            return self.after_value(step, eval, hooks);
                        }
                        Ok(None) => {}
                    }
                }
                let Some(payload) = node.payload() else {
                    return Err(planned_to_compute(node));
                };
                let slot = match self.gather(step, eval)? {
                    Gathered::Args(args) => {
                        let value = self.call(eval, hooks, node, payload, &args)?;
                        let absent = node.declines && eval.is_absent(&value);
                        Slot { value, absent }
                    }
                    Gathered::Skip(value) => {
                        let branch = self.branch_of(node);
                        hooks.check("skipped", |observer| observer.skipped(&node.name, branch))?;
                        Slot {
                            value,
                            absent: true,
                        }
                    }
                };
                self.store(step.node, slot);
                self.after_value(step, eval, hooks)
            }
            Action::UseInput | Action::SkipOptionalInput => Ok(true),
        }
    }

    fn store(&mut self, id: NodeId, slot: Slot<T>) {
        if let Some(place) = self.slots.get_mut(id.index()) {
            *place = Some(slot);
        }
    }

    /// What follows once a step's value is in its slot: nothing, unless it is an expand,
    /// whose value is iterated. `false` when that opened a block.
    fn after_value<V: Evaluate<P, Value = T>>(
        &mut self,
        step: &Step,
        eval: &mut V,
        hooks: &mut Hooks<'_, '_, T, V::Error>,
    ) -> Result<bool, RunError<V::Error>> {
        let graph = self.graph;
        let node = graph.node(step.node);
        if !node.is_expand() {
            return Ok(true);
        }
        let Some(slot) = self.slots.get(step.node.index()).and_then(Option::as_ref) else {
            return Err(broken(format!("expand '{}' has no value", node.name)));
        };
        if slot.absent {
            let value = slot.value.clone();
            self.absent_block(self.plan.block_of(step.node), &value);
            return Ok(true);
        }
        let Some(blocks) = eval.blocks() else {
            return Err(without_blocks(node));
        };
        let branch = self.branch_of(node);
        let iterator = blocks
            .iterate(node, &slot.value)
            .map_err(|source| node_error(graph, node, source, branch, Vec::new()))?;
        let Some(block) = self.plan.block_of(step.node) else {
            // Nothing in the request reads the block: pull the items, and keep what the
            // iterator returns once exhausted as the expand's value.
            let mut items = 0;
            let outcome = loop {
                match blocks.next_item(node, &iterator) {
                    Ok(Item::Next(_)) => items += 1,
                    Ok(Item::Done) => {
                        break blocks
                            .finish_items(node, &iterator, true)
                            .map(|value| Slot {
                                value,
                                absent: false,
                            });
                    }
                    Ok(Item::Declined(value)) => {
                        break Ok(Slot {
                            value,
                            absent: true,
                        });
                    }
                    Err(source) => {
                        drop(blocks.finish_items(node, &iterator, false));
                        break Err(source);
                    }
                }
            };
            let slot =
                outcome.map_err(|source| node_error(graph, node, source, branch, Vec::new()))?;
            let exhausted = !slot.absent;
            self.store(step.node, slot);
            if exhausted {
                let branch = self.branch_of(node);
                hooks.check("post_expand", |observer| {
                    observer.post_expand(&node.name, branch, items)
                })?;
            }
            return Ok(true);
        };
        let collected = self.plan.blocks()[block.index()].collected.len();
        self.frames.push(Frame {
            block,
            iterator,
            index: 0,
            position: 0,
            need: Vec::new(),
            collected: (0..collected).map(|_| Vec::new()).collect(),
        });
        self.branch.push(0);
        self.pull(eval, hooks)?;
        Ok(false)
    }

    /// Gives every collect of `block` the absent value of its expand, which has no
    /// branches.
    fn absent_block(&mut self, block: Option<BlockId>, value: &T) {
        let Some(block) = block else {
            return;
        };
        for &node in &self.plan.blocks()[block.index()].collected {
            self.gathered[node.index()] = Some(Collected::Absent(value.clone()));
        }
    }

    /// Ends the innermost frame's current branch: its collected values move to the frame.
    fn end_branch<E>(&mut self) -> Result<(), RunError<E>> {
        let plan = self.plan;
        let Some(frame) = self.frames.last_mut() else {
            return Ok(());
        };
        let sub = &plan.blocks()[frame.block.index()];
        for (kept, &node) in frame.collected.iter_mut().zip(&sub.collected) {
            let Some(slot) = self.slots.get_mut(node.index()).and_then(Option::take) else {
                return Err(broken(format!(
                    "collected node '{}' has no value at the end of a branch",
                    self.graph.node(node).name
                )));
            };
            kept.push(slot);
        }
        Ok(())
    }

    /// Pulls the innermost frame's next item: starts its branch, or ends the block.
    fn pull<V: Evaluate<P, Value = T>>(
        &mut self,
        eval: &mut V,
        hooks: &mut Hooks<'_, '_, T, V::Error>,
    ) -> Result<(), RunError<V::Error>> {
        let (plan, graph) = (self.plan, self.graph);
        let Some(frame) = self.frames.last_mut() else {
            return Ok(());
        };
        let sub: &SubPlan = &plan.blocks()[frame.block.index()];
        let node = graph.node(sub.expand);
        let Some(blocks) = eval.blocks() else {
            return Err(without_blocks(node));
        };
        let depth = graph.block_path(sub.expand).len();
        let item = blocks
            .next_item(node, &frame.iterator)
            .map_err(|source| node_error(graph, node, source, &self.branch[..depth], Vec::new()))?;
        match item {
            Item::Next(value) => {
                let index = frame.index;
                frame.index += 1;
                frame.position = 0;
                if let Some(last) = self.branch.last_mut() {
                    *last = index;
                }
                self.slots[sub.expand.index()] = Some(Slot {
                    value,
                    absent: false,
                });
                self.walk(eval, hooks)
            }
            Item::Done => {
                let Some(frame) = self.frames.pop() else {
                    return Ok(());
                };
                self.branch.pop();
                let value = blocks
                    .finish_items(node, &frame.iterator, true)
                    .map_err(|source| node_error(graph, node, source, &self.branch, Vec::new()))?;
                for (&collected, branches) in sub.collected.iter().zip(frame.collected) {
                    self.gathered[collected.index()] = Some(Collected::Branches(branches));
                }
                // An expand's value is wanted only as a requested output.
                self.slots[sub.expand.index()] = plan
                    .outputs()
                    .binary_search(&sub.expand)
                    .is_ok()
                    .then_some(Slot {
                        value,
                        absent: false,
                    });
                let branch = &self.branch;
                hooks.check("post_expand", |observer| {
                    observer.post_expand(&node.name, branch, frame.index)
                })?;
                self.complete_current();
                Ok(())
            }
            Item::Declined(value) => {
                // The branches already run are dropped with the frame.
                self.frames.pop();
                self.branch.pop();
                for &collected in &sub.collected {
                    self.gathered[collected.index()] = Some(Collected::Absent(value.clone()));
                }
                self.slots[sub.expand.index()] = Some(Slot {
                    value,
                    absent: true,
                });
                self.complete_current();
                Ok(())
            }
        }
    }

    /// Decides the innermost frame's needs for its new branch, then fires its hits.
    ///
    /// The walk goes back from the nodes the block's collects read, in reverse step order,
    /// asking `lookup` of each needed step: a hit's own arguments are not needed through it.
    /// A nested expand that is needed, hit or not, needs what its block reads.
    fn walk<V: Evaluate<P, Value = T>>(
        &mut self,
        eval: &mut V,
        hooks: &mut Hooks<'_, '_, T, V::Error>,
    ) -> Result<(), RunError<V::Error>> {
        let (plan, graph) = (self.plan, self.graph);
        let Some(frame) = self.frames.last_mut() else {
            return Ok(());
        };
        let sub = &plan.blocks()[frame.block.index()];
        frame.need.clear();
        frame.need.resize(sub.steps.len(), Need::Skip);
        for &node in &sub.collected {
            self.wanted[node.index()] = true;
        }
        let mut failure = None;
        for (position, step) in sub.steps.iter().enumerate().rev() {
            if !self.wanted[step.node.index()] {
                continue;
            }
            let node = graph.node(step.node);
            let hit = if step.action == Action::Compute {
                match eval.lookup(node, &self.branch) {
                    Ok(hit) => hit,
                    Err(source) => {
                        failure = Some(node_error(graph, node, source, &self.branch, Vec::new()));
                        break;
                    }
                }
            } else {
                None
            };
            if let Some(value) = hit {
                let absent = node.may_be_absent && eval.is_absent(&value);
                self.slots[step.node.index()] = Some(Slot { value, absent });
                frame.need[position] = Need::Hit;
            } else {
                frame.need[position] = Need::Run;
                for &arg in &step.args {
                    want(&mut self.wanted, graph, arg, true);
                }
            }
            if let Some(block) = plan.block_of(step.node) {
                for &read in &plan.blocks()[block.index()].reads {
                    want(&mut self.wanted, graph, read, true);
                }
            }
        }
        // Clear the scratch for the next walk.
        for &node in &sub.collected {
            self.wanted[node.index()] = false;
        }
        for step in &sub.steps {
            self.wanted[step.node.index()] = false;
            for &arg in &step.args {
                want(&mut self.wanted, graph, arg, false);
            }
            if let Some(block) = plan.block_of(step.node) {
                for &read in &plan.blocks()[block.index()].reads {
                    want(&mut self.wanted, graph, read, false);
                }
            }
        }
        if let Some(failure) = failure {
            return Err(failure);
        }
        let branch = &self.branch;
        for (step, &need) in sub.steps.iter().zip(&frame.need) {
            if need != Need::Hit {
                continue;
            }
            if let Some(slot) = self.slots.get(step.node.index()).and_then(Option::as_ref) {
                let name = &graph.node(step.node).name;
                hooks.check("hit", |observer| observer.hit_in(name, branch, &slot.value))?;
            }
        }
        Ok(())
    }

    /// A step's arguments by the absence rules, with each collect's argument gathered from
    /// its branches.
    fn gather<V: Evaluate<P, Value = T>>(
        &self,
        step: &Step,
        eval: &mut V,
    ) -> Result<Gathered<'r, T>, RunError<V::Error>> {
        let graph = self.graph;
        let consumer = graph.node(step.node);
        // Absence first, so a skipped node gathers nothing.
        for &id in &step.args {
            let edge = *edge_of(consumer, id)?;
            if let Some(value) = self.absent_value(consumer, edge)? {
                if edge.dependency == Dependency::Required {
                    return Ok(Gathered::Skip(value.clone()));
                }
            }
        }
        let mut args = Vec::with_capacity(step.args.len());
        for &id in &step.args {
            let name = graph.node(id).name.as_str();
            let edge = *edge_of(consumer, id)?;
            if let Some(value) = self.absent_value(consumer, edge)? {
                if edge.dependency != Dependency::Optional {
                    args.push(Arg {
                        node: id,
                        name,
                        value: value.clone(),
                    });
                }
                continue;
            }
            let value = match (
                edge.collect,
                &self.gathered[id.index()],
                &self.slots[id.index()],
            ) {
                (Some(mode), Some(Collected::Branches(branches)), _) => {
                    let values = branches
                        .iter()
                        .filter(|slot| mode == Collect::All || !slot.absent)
                        .map(|slot| slot.value.clone())
                        .collect();
                    let Some(blocks) = eval.blocks() else {
                        return Err(without_blocks(consumer));
                    };
                    blocks.gather(consumer, id, values).map_err(|source| {
                        node_error(
                            graph,
                            consumer,
                            source,
                            self.branch_of(consumer),
                            Vec::new(),
                        )
                    })?
                }
                (None, _, Some(slot)) => slot.value.clone(),
                _ => return Err(no_value(graph, consumer, id)),
            };
            args.push(Arg {
                node: id,
                name,
                value,
            });
        }
        Ok(Gathered::Args(args))
    }

    /// The absent value an edge reads, if it reads one: a whole absent block for a collect.
    fn absent_value<E>(&self, consumer: &Node<P>, edge: Edge) -> Result<Option<&T>, RunError<E>> {
        let id = edge.node;
        if edge.collect.is_some() {
            return match &self.gathered[id.index()] {
                Some(Collected::Absent(value)) => Ok(Some(value)),
                Some(Collected::Branches(_)) => Ok(None),
                None => Err(no_value(self.graph, consumer, id)),
            };
        }
        match self.slots.get(id.index()).and_then(Option::as_ref) {
            Some(slot) => Ok(slot.absent.then_some(&slot.value)),
            None => Err(no_value(self.graph, consumer, id)),
        }
    }

    /// Evaluates `node` on `args` between its `pre_node_in` and `post_node_in` events.
    fn call<V: Evaluate<P, Value = T>>(
        &self,
        eval: &mut V,
        hooks: &mut Hooks<'_, '_, T, V::Error>,
        node: &Node<P>,
        payload: &P,
        args: &[Arg<'_, T>],
    ) -> Result<T, RunError<V::Error>> {
        let branch = self.branch_of(node);
        if hooks.observers.is_empty() {
            return eval
                .evaluate_in(node, payload, args, branch)
                .map_err(|source| node_error(self.graph, node, source, branch, Vec::new()));
        }
        hooks.check("pre_node", |observer| {
            observer.pre_node_in(&node.name, branch, args)
        })?;
        match eval.evaluate_in(node, payload, args, branch) {
            Ok(value) => hooks
                .check("post_node", |observer| {
                    observer.post_node_in(&node.name, branch, Ok(&value))
                })
                .map(|()| value),
            Err(source) => {
                let errors =
                    hooks.fire(|observer| observer.post_node_in(&node.name, branch, Err(&source)));
                Err(node_error(self.graph, node, source, branch, errors))
            }
        }
    }

    /// The branch `node` runs in: the current path, cut to the depth of `node`'s block.
    fn branch_of(&self, node: &Node<P>) -> &[usize] {
        let depth = self.graph.block_path(node.id).len();
        &self.branch[..depth.min(self.branch.len())]
    }

    /// Finishes every open iterator as stopped, innermost first, after a failure.
    fn close<V: Evaluate<P, Value = T>>(&mut self, eval: &mut V) {
        while let Some(frame) = self.frames.pop() {
            let expand = self
                .graph
                .node(self.plan.blocks()[frame.block.index()].expand);
            if let Some(blocks) = eval.blocks() {
                // The run already failed; what closing returns is dropped.
                drop(blocks.finish_items(expand, &frame.iterator, false));
            }
        }
    }
}

/// Marks `arg` wanted (or clears it), with the expand of its block: a collected node of a
/// nested or sibling block is wanted through that block's expand step.
fn want<P>(wanted: &mut [bool], graph: &Graph<P>, arg: NodeId, to: bool) {
    wanted[arg.index()] = to;
    if let Some(expand) = graph.node(arg).block {
        wanted[expand.index()] = to;
    }
}

/// The edge of `consumer` that reads `id`.
fn edge_of<P, E>(consumer: &Node<P>, id: NodeId) -> Result<&Edge, RunError<E>> {
    consumer
        .inputs
        .iter()
        .find(|edge| edge.node == id)
        .ok_or_else(|| {
            broken(format!(
                "node '{}' has no edge it was planned to read",
                consumer.name
            ))
        })
}

/// A `RunError::Node` for `node`, failed in `branch`.
fn node_error<P, E>(
    graph: &Graph<P>,
    node: &Node<P>,
    source: E,
    branch: &[usize],
    observer_errors: Vec<E>,
) -> RunError<E> {
    RunError::Node {
        node: node.name.clone(),
        source,
        observer_errors,
        branch: branch.to_vec(),
        block: node.block.map(|expand| graph.node(expand).name.clone()),
    }
}

#[cold]
fn without_blocks<P, E>(node: &Node<P>) -> RunError<E> {
    broken(format!(
        "expand '{}' run by an evaluator without blocks",
        node.name
    ))
}

#[cold]
fn no_value<P, E>(graph: &Graph<P>, consumer: &Node<P>, id: NodeId) -> RunError<E> {
    broken(format!(
        "argument '{}' of node '{}' has no value",
        graph.node(id).name,
        consumer.name
    ))
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use std::collections::BTreeMap;
    use std::rc::Rc;

    use crate::graph::Graph;
    use crate::node::{Collect, Dependency, Node, NodeId, NodeSpec};
    use crate::plan::Plan;
    use crate::run::{Arg, Blocks, Evaluate, Item, Observer, RunError};
    use crate::testing::{Read, collect, expanding, plain, reading, request};

    /// How a test iterable ends once its items are pulled.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum End {
        Done,
        Decline,
        Fail,
    }

    /// The values of these tests. `Mark` counts live values through its `Rc`.
    #[derive(Clone, Debug, PartialEq)]
    enum Val {
        Num(i64),
        List(Vec<Val>),
        Gen(Vec<Val>, End),
        Iter(usize),
        Absent,
        Mark(i64, Rc<()>),
    }

    use Val::{Absent, List, Num};

    fn nums(values: &[i64]) -> Val {
        List(values.iter().map(|&n| Num(n)).collect())
    }

    fn items(values: &[i64]) -> Val {
        Val::Gen(values.iter().map(|&n| Num(n)).collect(), End::Done)
    }

    fn num(value: &Val) -> i64 {
        match value {
            Num(n) | Val::Mark(n, _) => *n,
            List(values) => values.iter().map(num).sum(),
            _ => 0,
        }
    }

    type Pay = Rc<dyn Fn(&[Val]) -> Result<Val, String>>;

    fn node(
        name: &str,
        reads: &[Read<'_>],
        body: impl Fn(&[Val]) -> Result<Val, String> + 'static,
    ) -> NodeSpec<Pay> {
        reading(name, reads, Rc::new(body) as Pay)
    }

    fn expand(
        name: &str,
        reads: &[Read<'_>],
        body: impl Fn(&[Val]) -> Result<Val, String> + 'static,
    ) -> NodeSpec<Pay> {
        expanding(name, reads, Rc::new(body) as Pay)
    }

    fn declines(spec: NodeSpec<Pay>) -> NodeSpec<Pay> {
        NodeSpec {
            declines: true,
            ..spec
        }
    }

    /// No branch at all: what `Fan::called` gives for a node that never ran.
    const NONE: [Vec<usize>; 0] = [];

    /// The sum of the arguments, times ten.
    #[expect(clippy::unnecessary_wraps, reason = "a payload returns a `Result`")]
    fn tens(args: &[Val]) -> Result<Val, String> {
        Ok(Num(10 * args.iter().map(num).sum::<i64>()))
    }

    /// The arguments as a list, one per argument.
    #[expect(clippy::unnecessary_wraps, reason = "a payload returns a `Result`")]
    fn echo(args: &[Val]) -> Result<Val, String> {
        Ok(List(args.to_vec()))
    }

    /// The single argument, unchanged.
    #[expect(clippy::unnecessary_wraps, reason = "a payload returns a `Result`")]
    fn first(args: &[Val]) -> Result<Val, String> {
        Ok(args.first().cloned().unwrap_or(List(Vec::new())))
    }

    /// One open iterator: its items, how it ends, and how many it gave.
    struct Open {
        items: Vec<Val>,
        end: End,
        given: usize,
    }

    /// Runs payloads and blocks, recording everything it is asked.
    #[derive(Default)]
    struct Fan {
        calls: Vec<(String, Vec<usize>)>,
        open: Vec<Open>,
        finished: Vec<(String, bool)>,
        asked: Vec<(String, Vec<usize>)>,
        hits: BTreeMap<(String, Vec<usize>), Val>,
        fail: Option<(&'static str, Vec<usize>)>,
    }

    impl Fan {
        fn called(&self, name: &str) -> Vec<Vec<usize>> {
            self.calls
                .iter()
                .filter(|(n, _)| n == name)
                .map(|(_, b)| b.clone())
                .collect()
        }
    }

    impl Evaluate<Pay> for Fan {
        type Value = Val;
        type Error = String;

        fn evaluate(
            &mut self,
            node: &Node<Pay>,
            payload: &Pay,
            args: &[Arg<'_, Val>],
        ) -> Result<Val, String> {
            self.evaluate_in(node, payload, args, &[])
        }

        fn evaluate_in(
            &mut self,
            node: &Node<Pay>,
            payload: &Pay,
            args: &[Arg<'_, Val>],
            branch: &[usize],
        ) -> Result<Val, String> {
            self.calls.push((node.name.clone(), branch.to_vec()));
            if self
                .fail
                .as_ref()
                .is_some_and(|(n, b)| *n == node.name && b == branch)
            {
                return Err(format!("{} failed", node.name));
            }
            let values: Vec<Val> = args.iter().map(|a| a.value.clone()).collect();
            payload(&values)
        }

        fn is_absent(&self, value: &Val) -> bool {
            *value == Absent
        }

        fn blocks(&mut self) -> Option<&mut dyn Blocks<Pay, Val, String>> {
            Some(self)
        }

        fn lookup(&mut self, node: &Node<Pay>, branch: &[usize]) -> Result<Option<Val>, String> {
            self.asked.push((node.name.clone(), branch.to_vec()));
            Ok(self
                .hits
                .get(&(node.name.clone(), branch.to_vec()))
                .cloned())
        }
    }

    impl Blocks<Pay, Val, String> for Fan {
        fn iterate(&mut self, node: &Node<Pay>, iterable: &Val) -> Result<Val, String> {
            let (items, end) = match iterable {
                Val::Gen(items, end) => (items.clone(), *end),
                List(items) => (items.clone(), End::Done),
                other => return Err(format!("{} gave {other:?}, not an iterable", node.name)),
            };
            self.open.push(Open {
                items,
                end,
                given: 0,
            });
            Ok(Val::Iter(self.open.len() - 1))
        }

        fn next_item(&mut self, node: &Node<Pay>, iterator: &Val) -> Result<Item<Val>, String> {
            let Val::Iter(at) = iterator else {
                return Err("not an iterator".to_owned());
            };
            let open = &mut self.open[*at];
            if let Some(item) = open.items.get(open.given) {
                open.given += 1;
                return Ok(Item::Next(item.clone()));
            }
            match open.end {
                End::Done => Ok(Item::Done),
                End::Decline => Ok(Item::Declined(Absent)),
                End::Fail => Err(format!("{} failed while iterating", node.name)),
            }
        }

        fn finish_items(
            &mut self,
            node: &Node<Pay>,
            iterator: &Val,
            exhausted: bool,
        ) -> Result<Val, String> {
            self.finished.push((node.name.clone(), exhausted));
            let Val::Iter(at) = iterator else {
                return Err("not an iterator".to_owned());
            };
            let open = &self.open[*at];
            Ok(List(open.items[..open.given].to_vec()))
        }

        fn gather(
            &mut self,
            _consumer: &Node<Pay>,
            _source: NodeId,
            values: Vec<Val>,
        ) -> Result<Val, String> {
            Ok(List(values))
        }
    }

    /// Logs every event with its branch.
    #[derive(Default)]
    struct Log {
        events: Vec<String>,
    }

    impl Observer<Val, String> for Log {
        fn pre_node_in(
            &mut self,
            node: &str,
            branch: &[usize],
            _args: &[Arg<'_, Val>],
        ) -> Result<(), String> {
            self.events.push(format!("pre:{node}:{branch:?}"));
            Ok(())
        }
        fn post_node_in(
            &mut self,
            node: &str,
            branch: &[usize],
            outcome: Result<&Val, &String>,
        ) -> Result<(), String> {
            self.events
                .push(format!("post:{node}:{branch:?}:{}", outcome.is_ok()));
            Ok(())
        }
        fn post_expand(
            &mut self,
            node: &str,
            branch: &[usize],
            items: usize,
        ) -> Result<(), String> {
            self.events
                .push(format!("expand:{node}:{branch:?}:{items}"));
            Ok(())
        }
        fn hit_in(&mut self, node: &str, branch: &[usize], _value: &Val) -> Result<(), String> {
            self.events.push(format!("hit:{node}:{branch:?}"));
            Ok(())
        }
        fn skipped(&mut self, node: &str, branch: &[usize]) -> Result<(), String> {
            self.events.push(format!("skipped:{node}:{branch:?}"));
            Ok(())
        }
    }

    type Outcome = Result<BTreeMap<String, Val>, RunError<String>>;

    fn run(
        graph: &Graph<Pay>,
        outputs: &[&str],
        overrides: &[(&str, Val)],
        eval: &mut Fan,
    ) -> Outcome {
        run_logged(graph, outputs, overrides, eval, &mut Log::default())
    }

    fn run_logged(
        graph: &Graph<Pay>,
        outputs: &[&str],
        overrides: &[(&str, Val)],
        eval: &mut Fan,
        log: &mut Log,
    ) -> Outcome {
        let names: Vec<&str> = overrides.iter().map(|(n, _)| *n).collect();
        let plan = graph.prepare(&request(outputs, &[], &names)).unwrap();
        let overrides: BTreeMap<String, Val> = overrides
            .iter()
            .map(|(n, v)| ((*n).to_owned(), v.clone()))
            .collect();
        plan.run(graph, eval, &BTreeMap::new(), &overrides, &mut [log])
    }

    fn out(pairs: &[(&str, Val)]) -> BTreeMap<String, Val> {
        pairs
            .iter()
            .map(|(n, v)| ((*n).to_owned(), v.clone()))
            .collect()
    }

    /// `picked` yields `of`; `details` is ten times each item plus the shared `zz_rate`;
    /// `total` collects `details`.
    fn fan(of: Val) -> Graph<Pay> {
        Graph::build(vec![
            expand("picked", &[], move |_| Ok(of.clone())),
            node("zz_rate", &[], |_| Ok(Num(1))),
            node("details", &[plain("picked"), plain("zz_rate")], |args| {
                Ok(Num(10 * num(&args[0]) + num(&args[1])))
            }),
            node("total", &[collect("details")], first),
        ])
        .unwrap()
    }

    #[test]
    fn branches_run_in_item_order_and_collect_in_branch_order() {
        let g = fan(items(&[3, 1, 2]));
        let mut eval = Fan::default();
        let result = run(&g, &["total"], &[], &mut eval).unwrap();
        assert_eq!(result, out(&[("total", nums(&[31, 11, 21]))]));
        let order: Vec<&str> = eval.calls.iter().map(|(n, _)| n.as_str()).collect();
        assert_eq!(
            order,
            [
                "zz_rate", "picked", "details", "details", "details", "total"
            ]
        );
        assert_eq!(eval.called("details"), [vec![0], vec![1], vec![2]]);
        assert_eq!(eval.finished, [("picked".to_owned(), true)]);
    }

    #[test]
    fn an_empty_expand_gives_empty_collects() {
        let g = fan(items(&[]));
        let mut eval = Fan::default();
        let result = run(&g, &["total"], &[], &mut eval).unwrap();
        assert_eq!(result, out(&[("total", nums(&[]))]));
        assert_eq!(eval.called("details"), NONE);
    }

    #[test]
    fn an_expand_requested_alone_returns_a_list() {
        // Q121: the output is what `finish_items` returns once the items are exhausted.
        let g = fan(items(&[4, 5]));
        let mut eval = Fan::default();
        let mut log = Log::default();
        let result = run_logged(&g, &["picked"], &[], &mut eval, &mut log).unwrap();
        assert_eq!(result, out(&[("picked", nums(&[4, 5]))]));
        assert_eq!(eval.finished, [("picked".to_owned(), true)]);
        assert_eq!(eval.called("details"), NONE);
        assert_eq!(log.events.last().unwrap(), "expand:picked:[]:2");
    }

    #[test]
    fn outer_values_are_computed_once_and_shared() {
        let g = fan(items(&[1, 2, 3]));
        let mut eval = Fan::default();
        run(&g, &["total"], &[], &mut eval).unwrap();
        assert_eq!(eval.called("zz_rate"), [Vec::<usize>::new()]);
    }

    #[test]
    fn an_expand_override_supplies_the_items() {
        let g = fan(items(&[1]));
        let mut eval = Fan::default();
        let result = run(&g, &["total"], &[("picked", nums(&[5, 6]))], &mut eval).unwrap();
        assert_eq!(result, out(&[("total", nums(&[51, 61]))]));
        assert_eq!(eval.called("picked"), NONE);
    }

    #[test]
    fn a_collect_override_cuts_the_block() {
        let g = fan(items(&[1]));
        let mut eval = Fan::default();
        let result = run(&g, &["total"], &[("total", Num(7))], &mut eval).unwrap();
        assert_eq!(result, out(&[("total", Num(7))]));
        assert_eq!(eval.calls, []);
    }

    /// `outer` yields `[2, 1]`; `inner` yields `0..k` for each outer item `k`; `leaf` is
    /// `10 * outer + inner`; `inner_sum` collects `leaf` per outer branch, `outer_sum`
    /// collects `inner_sum`.
    fn nested() -> Graph<Pay> {
        Graph::build(vec![
            expand("outer", &[], |_| Ok(items(&[2, 1]))),
            expand("inner", &[plain("outer")], |args| {
                let k = num(&args[0]);
                Ok(items(&(0..k).collect::<Vec<_>>()))
            }),
            node("leaf", &[plain("inner"), plain("outer")], |args| {
                Ok(Num(num(&args[0]) + 10 * num(&args[1])))
            }),
            node("inner_sum", &[collect("leaf")], first),
            node("outer_sum", &[collect("inner_sum")], first),
        ])
        .unwrap()
    }

    #[test]
    fn nested_blocks_gather_per_outer_branch() {
        let g = nested();
        let mut eval = Fan::default();
        let result = run(&g, &["outer_sum"], &[], &mut eval).unwrap();
        assert_eq!(
            result,
            out(&[("outer_sum", List(vec![nums(&[20, 21]), nums(&[10])]))])
        );
        assert_eq!(eval.called("leaf"), [vec![0, 0], vec![0, 1], vec![1, 0]]);
        assert_eq!(eval.called("inner"), [vec![0], vec![1]]);
        assert_eq!(eval.called("inner_sum"), [vec![0], vec![1]]);
    }

    #[test]
    fn a_branch_failure_closes_every_open_iterator_and_names_its_branch() {
        let g = nested();
        let mut eval = Fan {
            fail: Some(("leaf", vec![0, 1])),
            ..Fan::default()
        };
        let mut log = Log::default();
        let err = run_logged(&g, &["outer_sum"], &[], &mut eval, &mut log).unwrap_err();
        let RunError::Node {
            ref node,
            ref branch,
            ref block,
            ..
        } = err
        else {
            unreachable!("expected a node error, got {err}");
        };
        assert_eq!(
            (node.as_str(), branch.as_slice()),
            ("leaf", [0, 1].as_slice())
        );
        assert_eq!(block.as_deref(), Some("inner"));
        assert_eq!(
            err.to_string(),
            "node 'leaf' failed in branch (0, 1) of 'inner': leaf failed"
        );
        // Innermost first, and nothing after the failure ran.
        assert_eq!(
            eval.finished,
            [("inner".to_owned(), false), ("outer".to_owned(), false)]
        );
        assert_eq!(eval.called("leaf").len(), 2);
        assert_eq!(log.events.last().unwrap(), "post:leaf:[0, 1]:false");
    }

    #[test]
    fn a_failing_iterator_closes_the_outer_ones_and_is_its_expands_failure() {
        let g = Graph::build(vec![
            expand("outer", &[], |_| Ok(items(&[1]))),
            expand("inner", &[plain("outer")], |_| {
                Ok(Val::Gen(vec![Num(1)], End::Fail))
            }),
            node("leaf", &[plain("inner")], tens),
            node("inner_sum", &[collect("leaf")], first),
            node("outer_sum", &[collect("inner_sum")], first),
        ])
        .unwrap();
        let mut eval = Fan::default();
        let err = run(&g, &["outer_sum"], &[], &mut eval).unwrap_err();
        assert_eq!(
            err.to_string(),
            "node 'inner' failed in branch (0,) of 'outer': inner failed while iterating"
        );
        assert_eq!(
            eval.finished,
            [("inner".to_owned(), false), ("outer".to_owned(), false)]
        );
    }

    #[test]
    fn collected_values_are_kept_and_branch_locals_released() {
        // The counts held outside any value: the test's own, and one per payload closure.
        const HOLDERS: usize = 5;
        let marker = Rc::new(());
        let live = Rc::new(std::cell::RefCell::new(Vec::new()));
        let seen = |name: &'static str,
                    marker: &Rc<()>,
                    live: &Rc<std::cell::RefCell<Vec<(String, usize)>>>| {
            let marker = Rc::clone(marker);
            let live = Rc::clone(live);
            move |args: &[Val]| {
                live.borrow_mut()
                    .push((name.to_owned(), Rc::strong_count(&marker) - HOLDERS));
                Ok(Val::Mark(args.iter().map(num).sum(), Rc::clone(&marker)))
            }
        };
        let g = Graph::build(vec![
            expand("picked", &[], |_| Ok(items(&[1, 2, 3]))),
            node("a", &[plain("picked")], seen("a", &marker, &live)),
            node("b", &[plain("a")], seen("b", &marker, &live)),
            node("c", &[plain("b")], seen("c", &marker, &live)),
            node("total", &[collect("c")], {
                let marker = Rc::clone(&marker);
                let live = Rc::clone(&live);
                move |args: &[Val]| {
                    live.borrow_mut()
                        .push(("total".to_owned(), Rc::strong_count(&marker) - HOLDERS));
                    Ok(Num(num(&args[0])))
                }
            }),
        ])
        .unwrap();
        let mut eval = Fan::default();
        let result = run(&g, &["total"], &[], &mut eval).unwrap();
        assert_eq!(result, out(&[("total", Num(6))]));
        // When each node of branch i starts: the i values of `c` kept so far, plus its
        // argument's slot, the clone passed to it, and `Fan`'s own copy of its arguments; `a`
        // reads an item, which is no `Mark`. At `total`: the three kept, the gathered list
        // passed to it, and `Fan`'s copy. Were a branch's `a` or `b` kept past its last use,
        // the next branch would see more.
        let expected: Vec<(String, usize)> = [
            ("a", 0),
            ("b", 3),
            ("c", 3),
            ("a", 1),
            ("b", 4),
            ("c", 4),
            ("a", 2),
            ("b", 5),
            ("c", 5),
            ("total", 9),
        ]
        .iter()
        .map(|&(n, c)| (n.to_owned(), c))
        .collect();
        assert_eq!(*live.borrow(), expected);
        drop(g);
        assert_eq!(Rc::strong_count(&marker), 1);
    }

    /// `details` declines for the item 2; `total` keeps the present branches, `every`
    /// every branch.
    fn declining_details() -> Graph<Pay> {
        Graph::build(vec![
            expand("picked", &[], |_| Ok(items(&[1, 2, 3]))),
            declines(node("details", &[plain("picked")], |args| {
                if num(&args[0]) == 2 {
                    Ok(Absent)
                } else {
                    tens(args)
                }
            })),
            node("total", &[collect("details")], first),
            node(
                "every",
                &[("details", Dependency::Required, Some(Collect::All))],
                first,
            ),
        ])
        .unwrap()
    }

    #[test]
    fn collect_present_drops_absent_branches() {
        let g = declining_details();
        let result = run(&g, &["total"], &[], &mut Fan::default()).unwrap();
        assert_eq!(result, out(&[("total", nums(&[10, 30]))]));
    }

    #[test]
    fn collect_all_keeps_their_places() {
        let g = declining_details();
        let result = run(&g, &["every"], &[], &mut Fan::default()).unwrap();
        assert_eq!(
            result,
            out(&[("every", List(vec![Num(10), Absent, Num(30)]))])
        );
    }

    /// `picked` yields `of` and may decline; `req`, `opt` and `adm` collect `details` with
    /// each of the three dependencies.
    fn absent_expand(of: Val) -> Graph<Pay> {
        Graph::build(vec![
            declines(expand("picked", &[], move |_| Ok(of.clone()))),
            node("details", &[plain("picked")], tens),
            node("req", &[collect("details")], echo),
            node(
                "opt",
                &[("details", Dependency::Optional, Some(Collect::Present))],
                echo,
            ),
            node(
                "adm",
                &[(
                    "details",
                    Dependency::Admits { optional: false },
                    Some(Collect::Present),
                )],
                echo,
            ),
        ])
        .unwrap()
    }

    #[test]
    fn an_absent_expand_follows_the_three_rules_per_collect() {
        let g = absent_expand(Absent);
        let mut eval = Fan::default();
        let mut log = Log::default();
        let result = run_logged(
            &g,
            &["req", "opt", "adm", "picked"],
            &[],
            &mut eval,
            &mut log,
        )
        .unwrap();
        assert_eq!(
            result,
            out(&[
                ("adm", List(vec![Absent])),
                ("opt", List(Vec::new())),
                ("picked", Absent),
                ("req", Absent),
            ])
        );
        assert!(eval.called("details").is_empty() && eval.finished.is_empty());
        assert!(log.events.contains(&"skipped:req:[]".to_owned()));
        assert!(!log.events.iter().any(|e| e.starts_with("expand:")));
    }

    #[test]
    fn an_expand_declining_midway_drops_the_branches_already_run() {
        let g = absent_expand(Val::Gen(vec![Num(1), Num(2)], End::Decline));
        let mut eval = Fan::default();
        let result = run(&g, &["req", "adm"], &[], &mut eval).unwrap();
        assert_eq!(result, out(&[("adm", List(vec![Absent])), ("req", Absent)]));
        assert_eq!(eval.called("details").len(), 2);
        assert_eq!(eval.finished, []);
    }

    /// An evaluator with no blocks.
    struct Plain;

    impl Evaluate<Pay> for Plain {
        type Value = Val;
        type Error = String;

        fn evaluate(
            &mut self,
            _node: &Node<Pay>,
            payload: &Pay,
            args: &[Arg<'_, Val>],
        ) -> Result<Val, String> {
            let values: Vec<Val> = args.iter().map(|a| a.value.clone()).collect();
            payload(&values)
        }
    }

    #[test]
    fn an_evaluator_without_blocks_gets_a_broken_plan_error() {
        let g = fan(items(&[1]));
        for output in ["total", "picked"] {
            let plan = g.prepare(&request(&[output], &[], &[])).unwrap();
            let err = plan
                .run(&g, &mut Plain, &BTreeMap::new(), &BTreeMap::new(), &mut [])
                .unwrap_err();
            assert_eq!(
                err.to_string(),
                "broken plan: expand 'picked' run by an evaluator without blocks"
            );
        }
    }

    #[test]
    fn observer_events_carry_branch_paths_in_reference_order() {
        let g = fan(items(&[7, 8]));
        let mut log = Log::default();
        run_logged(&g, &["total"], &[], &mut Fan::default(), &mut log).unwrap();
        assert_eq!(
            log.events,
            [
                "pre:zz_rate:[]",
                "post:zz_rate:[]:true",
                "pre:picked:[]",
                "post:picked:[]:true",
                "pre:details:[0]",
                "post:details:[0]:true",
                "pre:details:[1]",
                "post:details:[1]:true",
                "expand:picked:[]:2",
                "pre:total:[]",
                "post:total:[]:true",
            ]
        );
    }

    /// `a` reads each item, `b` reads `a`, `c` reads `a`; `total` collects `b` and `count`
    /// collects `c`.
    fn chain() -> Graph<Pay> {
        Graph::build(vec![
            expand("picked", &[], |_| Ok(items(&[1, 2, 3]))),
            node("a", &[plain("picked")], tens),
            node("b", &[plain("a")], tens),
            node("c", &[plain("a")], tens),
            node("total", &[collect("b")], first),
            node("count", &[collect("c")], first),
        ])
        .unwrap()
    }

    fn hit(name: &str, branch: &[usize], value: Val) -> ((String, Vec<usize>), Val) {
        ((name.to_owned(), branch.to_vec()), value)
    }

    fn asked(pairs: &[(&str, &[usize])]) -> Vec<(String, Vec<usize>)> {
        pairs
            .iter()
            .map(|&(n, b)| (n.to_owned(), b.to_vec()))
            .collect()
    }

    #[test]
    fn lookup_is_asked_in_reverse_need_order_and_a_hit_skips_upstream() {
        let g = chain();
        let mut eval = Fan {
            hits: BTreeMap::from([hit("b", &[1], Num(-1))]),
            ..Fan::default()
        };
        let result = run(&g, &["total"], &[], &mut eval).unwrap();
        assert_eq!(
            result,
            out(&[("total", List(vec![Num(100), Num(-1), Num(300)]))])
        );
        assert_eq!(
            eval.asked,
            asked(&[
                ("b", &[0]),
                ("a", &[0]),
                ("b", &[1]),
                ("b", &[2]),
                ("a", &[2]),
                ("total", &[]),
            ])
        );
        assert_eq!(eval.called("a"), [vec![0], vec![2]]);
        // A hit outside a block skips only that step.
        let mut eval = Fan {
            hits: BTreeMap::from([hit("total", &[], Num(9))]),
            ..Fan::default()
        };
        let result = run(&g, &["total"], &[], &mut eval).unwrap();
        assert_eq!(result, out(&[("total", Num(9))]));
        assert_eq!(eval.called("b").len(), 3);
        assert_eq!(eval.called("total"), NONE);
    }

    #[test]
    fn hit_in_fires_for_each_hit_in_plan_order() {
        let g = chain();
        let mut eval = Fan {
            hits: BTreeMap::from([
                hit("c", &[0], Num(5)),
                hit("b", &[0], Num(4)),
                hit("count", &[], Num(0)),
            ]),
            ..Fan::default()
        };
        let mut log = Log::default();
        let result = run_logged(&g, &["total", "count"], &[], &mut eval, &mut log).unwrap();
        assert_eq!(result["total"], List(vec![Num(4), Num(200), Num(300)]));
        assert_eq!(result["count"], Num(0));
        let hits: Vec<&String> = log
            .events
            .iter()
            .filter(|e| e.starts_with("hit:"))
            .collect();
        assert_eq!(hits, ["hit:b:[0]", "hit:c:[0]", "hit:count:[]"]);
        // The hits of branch 0 fire before its first step would run, and `a` doesn't.
        assert_eq!(log.events[2..4], ["hit:b:[0]", "hit:c:[0]"]);
        assert_eq!(eval.called("a"), [vec![1], vec![2]]);
    }

    #[test]
    fn a_lookup_hit_that_is_absent_skips_its_consumers() {
        let g = Graph::build(vec![
            expand("picked", &[], |_| Ok(items(&[1, 2]))),
            declines(node("a", &[plain("picked")], tens)),
            node("b", &[plain("a")], tens),
            node("total", &[collect("b")], first),
        ])
        .unwrap();
        let mut eval = Fan {
            hits: BTreeMap::from([hit("a", &[0], Absent)]),
            ..Fan::default()
        };
        let mut log = Log::default();
        let result = run_logged(&g, &["total"], &[], &mut eval, &mut log).unwrap();
        assert_eq!(result, out(&[("total", nums(&[200]))]));
        assert!(log.events.contains(&"skipped:b:[0]".to_owned()));
        assert_eq!(eval.called("b"), [vec![1]]);
    }

    #[test]
    fn a_node_may_collect_one_block_and_expand_another() {
        // Q136: `mid` collects the block of `a` and expands its own.
        let g = Graph::build(vec![
            expand("a", &[], |_| Ok(items(&[1, 2]))),
            node("b", &[plain("a")], tens),
            expand("mid", &[collect("b")], |args| Ok(args[0].clone())),
            node("c", &[plain("mid")], tens),
            node("out", &[collect("c")], first),
        ])
        .unwrap();
        let result = run(&g, &["out"], &[], &mut Fan::default()).unwrap();
        assert_eq!(result, out(&[("out", nums(&[100, 200]))]));
    }

    /// `zb` yields `[1, 2]` and `zb_m` is ten times each; inside the block of `ca`, `n`
    /// collects the sibling block of `zb`; `total` collects `n`. Named so that `ca`'s block
    /// sorts before `zb`'s, which it must still wait for.
    fn sibling_collect(inside: Option<&'static str>) -> Vec<NodeSpec<Pay>> {
        let reads_of = |name: &'static str| -> Vec<Read<'static>> {
            inside.map(plain).into_iter().chain([plain(name)]).collect()
        };
        vec![
            expand("zb", &reads_of("seed"), |_| Ok(items(&[1, 2]))),
            node("zb_m", &[plain("zb")], tens),
            expand("ca", &reads_of("seed"), |_| Ok(items(&[5, 6]))),
            node("n", &[plain("ca"), collect("zb_m")], |args| {
                Ok(args[1].clone())
            }),
            node("total", &[collect("n")], first),
        ]
    }

    #[test]
    fn a_block_collecting_a_sibling_block_waits_for_it() {
        let mut specs = sibling_collect(None);
        specs.push(node("seed", &[], |_| Ok(Num(0))));
        let g = Graph::build(specs).unwrap();
        let plan = g.prepare(&request(&["total"], &[], &[])).unwrap();
        let outer: Vec<&str> = plan
            .steps()
            .iter()
            .map(|s| g.node(s.node).name.as_str())
            .collect();
        assert_eq!(outer, ["seed", "zb", "ca", "total"]);
        // The gathered values of `zb_m` are released after the block that reads them.
        let ca = &plan.steps()[2];
        assert!(ca.release.contains(&g.id_of("zb_m").unwrap()));
        let result = run(&g, &["total"], &[], &mut Fan::default()).unwrap();
        assert_eq!(
            result,
            out(&[("total", List(vec![nums(&[10, 20]), nums(&[10, 20])]))])
        );
    }

    #[test]
    fn a_nested_block_collecting_a_sibling_block_waits_for_it() {
        let mut specs = sibling_collect(Some("outer"));
        specs.push(expand("outer", &[], |_| Ok(items(&[1]))));
        specs.push(node("seed", &[plain("outer")], |_| Ok(Num(0))));
        specs.push(node("all", &[collect("total")], first));
        let g = Graph::build(specs).unwrap();
        let result = run(&g, &["all"], &[], &mut Fan::default()).unwrap();
        assert_eq!(
            result,
            out(&[(
                "all",
                List(vec![List(vec![nums(&[10, 20]), nums(&[10, 20])])])
            )])
        );
    }

    #[test]
    fn no_recursion_on_a_1000_deep_nesting_or_a_50000_chain_in_a_block() {
        let depth = 1_000;
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
        let g = Graph::build(specs).unwrap();
        let result = run(&g, &["c0000"], &[], &mut Fan::default()).unwrap();
        assert_eq!(num(&result["c0000"]), 1);

        let mut specs = vec![
            expand("e", &[], |_| Ok(items(&[1, 2]))),
            node("n00000", &[plain("e")], first),
        ];
        specs.extend((1..50_000).map(|i| {
            node(
                &format!("n{i:05}"),
                &[plain(&format!("n{:05}", i - 1))],
                first,
            )
        }));
        specs.push(node("total", &[collect("n49999")], first));
        let g = Graph::build(specs).unwrap();
        let result = run(&g, &["total"], &[], &mut Fan::default()).unwrap();
        assert_eq!(result["total"], nums(&[1, 2]));
    }

    #[test]
    fn a_plan_without_an_expand_runs_without_blocks() {
        // The plain loop: an evaluator without blocks runs a plan of a graph with a block
        // that the request doesn't reach.
        let g = fan(items(&[1]));
        let plan: Plan = g.prepare(&request(&["zz_rate"], &[], &[])).unwrap();
        assert!(!plan.has_expands());
        let result = plan
            .run(&g, &mut Plain, &BTreeMap::new(), &BTreeMap::new(), &mut [])
            .unwrap();
        assert_eq!(result, out(&[("zz_rate", Num(1))]));
    }
}
