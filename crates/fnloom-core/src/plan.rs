//! Turning a request into a validated, reusable execution plan.

use std::cmp::Reverse;
use std::collections::{BTreeSet, BinaryHeap};

use crate::error::{MissingInput, OutputInBlock, PlanError};
use crate::graph::Graph;
use crate::node::{Node, NodeId, NodeKind};

/// What to compute and what the caller supplies.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Request {
    /// Names of the nodes whose values are wanted.
    pub outputs: Vec<String>,
    /// Names of `Input` nodes the caller will supply.
    pub inputs: BTreeSet<String>,
    /// Names of nodes whose values the caller supplies in place of computing them.
    pub overrides: BTreeSet<String>,
}

/// What the executor does for one planned node.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    /// Take the caller's override value.
    UseOverride,
    /// Take the caller's supplied input value.
    UseInput,
    /// An optional input that was not supplied; consumers receive no argument for it.
    SkipOptionalInput,
    /// Run the node's payload on its arguments.
    Compute,
}

/// One unit of work in a plan.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Step {
    /// The node this step produces.
    pub node: NodeId,
    /// What to do for the node.
    pub action: Action,
    /// Nodes whose values are passed in, sorted by id (equivalently by name).
    pub args: Vec<NodeId>,
    /// Nodes whose value is dead once this step has run and that are not requested outputs.
    pub release: Vec<NodeId>,
}

/// Identifier of a sub-plan: its position in `Plan::blocks`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct BlockId(pub u32);

impl BlockId {
    /// Position of this sub-plan in `Plan::blocks`.
    #[must_use]
    pub const fn index(self) -> usize {
        self.0 as usize
    }
}

/// The steps of one block, run once per item of its expand.
///
/// A nested block is one step of its outer sub-plan: the step of its expand, whose own
/// sub-plan `Plan::block_of` names.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SubPlan {
    /// The expand whose items run the block; its slot holds the branch's item.
    pub expand: NodeId,
    /// The steps of one branch, in order. A position in it is a step's order key within a
    /// branch.
    pub steps: Vec<Step>,
    /// The block nodes that collects read, sorted: each branch's value of these is kept.
    pub collected: Vec<NodeId>,
    /// The values from outside the block that its nodes read, nested blocks included,
    /// sorted. They are computed before the expand and shared by every branch. A node of a
    /// sibling block that a node inside collects is one too: its gathered values are.
    pub reads: Vec<NodeId>,
}

/// A validated execution order for one request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    steps: Vec<Step>,
    outputs: Vec<NodeId>,
    needs_rules: bool,
    blocks: Vec<SubPlan>,
    expands: bool,
    /// Per node, the sub-plan its expand step runs; empty without an expand.
    block_of: Vec<Option<BlockId>>,
    /// Per node, whether its step is late; empty without an expand.
    late: Vec<bool>,
    /// For each of a step's `args`, the index of the edge in `Node::inputs` that reads it,
    /// packed; per node, its step's span in it. Both empty without an expand.
    arg_edges: Vec<u32>,
    arg_spans: Vec<(u32, u32)>,
}

impl Plan {
    /// The steps in execution order.
    #[must_use]
    pub fn steps(&self) -> &[Step] {
        &self.steps
    }

    /// The requested outputs, de-duplicated and sorted by id.
    #[must_use]
    pub fn outputs(&self) -> &[NodeId] {
        &self.outputs
    }

    /// Whether a planned node's value may be absent (`Node::may_be_absent`), so that the run
    /// follows the absence rules. A plan without one runs the plain loop, at no extra cost.
    #[must_use]
    pub const fn needs_rules(&self) -> bool {
        self.needs_rules
    }

    /// The sub-plans of the blocks the request reaches, nested ones included; each expand
    /// step's own is named by `Plan::block_of`.
    #[must_use]
    pub fn blocks(&self) -> &[SubPlan] {
        &self.blocks
    }

    /// The sub-plan the step of `expand` runs once per item, computed or overridden; `None`
    /// for any other node, and for an expand whose block the request doesn't reach: its
    /// items are then pulled and returned as its value.
    ///
    /// Kept beside the steps rather than in `Step`, so a step stays the size it is in a
    /// plan without blocks.
    #[must_use]
    pub fn block_of(&self, expand: NodeId) -> Option<BlockId> {
        self.block_of.get(expand.index()).copied().flatten()
    }

    /// Whether the step of `node` is downstream of an expand, so its cache key is known
    /// only during the run: `Evaluate::lookup` is asked for it before it computes. Every
    /// step of a sub-plan is.
    #[must_use]
    pub fn is_late(&self, node: NodeId) -> bool {
        self.late.get(node.index()).copied().unwrap_or(false)
    }

    /// For the step of `node`, the edges its `args` are read by, as indices into
    /// `Node::inputs`, in `args` order; empty in a plan without an expand.
    pub(crate) fn arg_edges(&self, node: NodeId) -> &[u32] {
        self.arg_spans
            .get(node.index())
            .and_then(|&(start, end)| self.arg_edges.get(start as usize..end as usize))
            .unwrap_or_default()
    }

    /// Whether a step is an expand, so the run iterates and runs blocks. A plan without one
    /// runs the plain loop.
    #[must_use]
    pub const fn has_expands(&self) -> bool {
        self.expands
    }
}

/// A request whose names have been resolved to ids.
struct Resolved {
    /// The requested outputs, sorted and de-duplicated.
    outputs: Vec<NodeId>,
    /// Whether the caller supplies each node, by id.
    supplied: Vec<bool>,
    /// Whether the caller overrides each node, by id.
    overrides: Vec<bool>,
}

impl<P> Graph<P> {
    /// Validates `request` and orders the work it needs.
    ///
    /// Only nodes needed by the outputs are planned; an override cuts the dependencies behind
    /// it. Steps run in topological order, ties broken by node id (that is, by name).
    ///
    /// # Errors
    ///
    /// The first non-empty category of `UnknownOutputs`, `UnknownInputs`, `UnknownOverrides`,
    /// `OutputsInBlocks`, `OverridesInBlocks`, `MissingInputs`, each listing its names sorted
    /// and de-duplicated.
    pub fn prepare(&self, request: &Request) -> Result<Plan, PlanError> {
        let resolved = self.resolve_request(request)?;
        if self.has_blocks() {
            self.refuse_in_blocks(&resolved)?;
        }
        let actions = self.plan_actions(&resolved)?;
        if self.has_blocks()
            && self
                .nodes()
                .iter()
                .zip(&actions)
                .any(|(node, action)| action.is_some() && node.is_expand())
        {
            return Ok(self.plan_blocks(&actions, resolved));
        }
        let order = self.topological_order(&actions);
        let needs_rules = self.has_absence() && order.iter().any(|&id| self.node(id).may_be_absent);
        Ok(Plan {
            steps: self.build_steps(&order, &actions, &resolved.outputs),
            outputs: resolved.outputs,
            needs_rules,
            blocks: Vec::new(),
            expands: false,
            block_of: Vec::new(),
            late: Vec::new(),
            arg_edges: Vec::new(),
            arg_spans: Vec::new(),
        })
    }

    fn resolve_request(&self, request: &Request) -> Result<Resolved, PlanError> {
        let outputs = self.lookup(
            request.outputs.iter().map(String::as_str),
            PlanError::UnknownOutputs,
            |_| true,
        )?;
        let supplied = self.lookup(
            request.inputs.iter().map(String::as_str),
            PlanError::UnknownInputs,
            Node::is_input,
        )?;
        let overrides = self.lookup(
            request.overrides.iter().map(String::as_str),
            PlanError::UnknownOverrides,
            |_| true,
        )?;
        Ok(Resolved {
            outputs,
            supplied: self.mask(&supplied),
            overrides: self.mask(&overrides),
        })
    }

    /// `OutputsInBlocks` for requested outputs inside blocks, then `OverridesInBlocks` for
    /// overrides of nodes inside blocks: each has one value per branch.
    #[cold]
    #[inline(never)]
    fn refuse_in_blocks(&self, resolved: &Resolved) -> Result<(), PlanError> {
        let outputs: Vec<OutputInBlock> = resolved
            .outputs
            .iter()
            .filter_map(|&id| {
                let expand = self.node(id).block?;
                let collects = self
                    .dependents(id)
                    .iter()
                    .filter(|&&consumer| {
                        self.node(consumer)
                            .inputs
                            .iter()
                            .any(|edge| edge.node == id && edge.collect.is_some())
                    })
                    .map(|&consumer| self.node(consumer).name.clone())
                    .collect();
                Some(OutputInBlock {
                    node: self.node(id).name.clone(),
                    expand: self.node(expand).name.clone(),
                    collects,
                })
            })
            .collect();
        if !outputs.is_empty() {
            return Err(PlanError::OutputsInBlocks(outputs));
        }
        let overrides: Vec<String> = self
            .nodes()
            .iter()
            .filter(|node| resolved.overrides[node.id.index()] && node.block.is_some())
            .map(|node| node.name.clone())
            .collect();
        if overrides.is_empty() {
            Ok(())
        } else {
            Err(PlanError::OverridesInBlocks(overrides))
        }
    }

    /// The plan of a request that reaches an expand: the outer steps, and a sub-plan for
    /// each block the request reaches.
    ///
    /// Each scope, the outer plan or one block, is ordered by Kahn's algorithm over its own
    /// units, ties broken by id: a node of the scope stands for itself, and a nested block
    /// for its expand. A unit waits for the units its inputs stand for in its scope; an
    /// expand also waits for those its block reads, so a value read only inside the block
    /// is computed first. A collect thus waits for the expand of the block it collects, and
    /// a block's own expand, outside its scope, counts as done.
    ///
    /// A value is released in the scope it lives in, after the last step there that reads
    /// it; an expand step reads what its block reads. A node a collect reads is kept for the
    /// whole branch, then gathered, and released after the last collect reading it.
    #[inline(never)]
    fn plan_blocks(&self, actions: &[Option<Action>], resolved: Resolved) -> Plan {
        let count = self.nodes().len();
        // The blocks the request reaches, numbered in the order of their expands' ids.
        let mut entered = vec![false; count];
        for node in self.nodes() {
            if actions[node.id.index()].is_some() {
                if let Some(expand) = node.block {
                    entered[expand.index()] = true;
                }
            }
        }
        let mut block_of: Vec<Option<BlockId>> = vec![None; count];
        let mut expands: Vec<NodeId> = Vec::new();
        for node in self.nodes() {
            if entered[node.id.index()] {
                block_of[node.id.index()] = Some(BlockId(index_u32(expands.len())));
                expands.push(node.id);
            }
        }
        let (reads, collected, is_collected) = self.block_reads(actions, &block_of);
        let orders = self.scope_orders(actions, &block_of, &reads);
        let mut late = vec![false; count];
        let mut scopes: Vec<Vec<Step>> = Vec::with_capacity(orders.len());
        for (at, order) in orders.iter().enumerate() {
            let mut steps = Vec::with_capacity(order.len());
            for &id in order {
                let Some(action) = actions[id.index()] else {
                    continue;
                };
                let args = self.passed_args(id, action, actions);
                let is_late = at > 0
                    || (action == Action::Compute
                        && (self.node(id).inputs.iter().any(|e| e.collect.is_some())
                            || args.iter().any(|arg| late[arg.index()])));
                late[id.index()] = is_late;
                steps.push(Step {
                    node: id,
                    action,
                    args,
                    release: Vec::new(),
                });
            }
            scopes.push(steps);
        }

        self.release_in_scopes(
            &mut scopes,
            &expands,
            &block_of,
            &reads,
            &is_collected,
            &resolved.outputs,
        );

        let mut arg_edges: Vec<u32> = Vec::new();
        let mut arg_spans = vec![(0, 0); count];
        for step in scopes.iter().flatten() {
            let start = index_u32(arg_edges.len());
            self.push_edges_of_args(step, &mut arg_edges);
            arg_spans[step.node.index()] = (start, index_u32(arg_edges.len()));
        }
        let needs_rules = self.has_absence()
            && self
                .nodes()
                .iter()
                .any(|node| actions[node.id.index()].is_some() && node.may_be_absent);
        let mut scopes = scopes.into_iter();
        let steps = scopes.next().unwrap_or_default();
        let blocks = expands
            .iter()
            .zip(scopes)
            .zip(reads.into_iter().zip(collected))
            .map(|((&expand, steps), (reads, collected))| SubPlan {
                expand,
                steps,
                collected,
                reads,
            })
            .collect();
        Plan {
            steps,
            outputs: resolved.outputs,
            needs_rules,
            blocks,
            expands: true,
            block_of,
            late,
            arg_edges,
            arg_spans,
        }
    }

    /// Pushes, for each of `step`'s `args`, sorted by id, the index of the edge of its node
    /// that reads it: a merge of the args with the edges sorted by source. Each source has one
    /// edge, since a node reading one source both ways is refused at build.
    fn push_edges_of_args(&self, step: &Step, into: &mut Vec<u32>) {
        let inputs = &self.node(step.node).inputs;
        let mut by_source: Vec<(NodeId, u32)> = inputs
            .iter()
            .enumerate()
            .map(|(at, edge)| (edge.node, index_u32(at)))
            .collect();
        by_source.sort_unstable();
        let mut sources = by_source.into_iter().peekable();
        into.extend(step.args.iter().filter_map(|&arg| {
            while sources.next_if(|&(source, _)| source < arg).is_some() {}
            sources
                .next_if(|&(source, _)| source == arg)
                .map(|(_, at)| at)
        }));
    }

    /// Each scope's units in order: the outer plan's first, then each block's by id.
    ///
    /// One Kahn's algorithm over every planned unit, popping the smallest id first: a unit
    /// waits only for units of its own scope, so each scope's order is its own.
    fn scope_orders(
        &self,
        actions: &[Option<Action>],
        block_of: &[Option<BlockId>],
        reads: &[Vec<NodeId>],
    ) -> Vec<Vec<NodeId>> {
        let scope = |id: NodeId| {
            self.node(id)
                .block
                .and_then(|expand| block_of[expand.index()])
                .map_or(0, |block| block.index() + 1)
        };

        let count = self.nodes().len();
        // The contracted dependencies, as forward edges and waiting counts.
        let mut waiting = vec![0_usize; count];
        let mut next: Vec<Vec<NodeId>> = vec![Vec::new(); count];
        for node in self.nodes() {
            let Some(action) = actions[node.id.index()] else {
                continue;
            };
            let depth = self.block_path(node.id).len();
            let mut wait = |source: NodeId| {
                if let Some(unit) = self.unit_in(source, depth) {
                    if unit != node.id {
                        waiting[node.id.index()] += 1;
                        next[unit.index()].push(node.id);
                    }
                }
            };
            if action == Action::Compute {
                for edge in &node.inputs {
                    // A collect reads its source's block as a whole, once that has run.
                    let place = match (edge.collect, self.node(edge.node).block) {
                        (Some(_), Some(expand)) => expand,
                        _ => edge.node,
                    };
                    wait(place);
                }
            }
            if let Some(block) = block_of[node.id.index()] {
                for &read in &reads[block.index()] {
                    wait(self.read_place(read, node.id));
                }
            }
        }
        let mut queue: BinaryHeap<Reverse<NodeId>> = self
            .nodes()
            .iter()
            .filter(|node| actions[node.id.index()].is_some() && waiting[node.id.index()] == 0)
            .map(|node| Reverse(node.id))
            .collect();
        // Every scope's order at once: its units only wait for one another.
        let mut orders: Vec<Vec<NodeId>> = vec![Vec::new(); reads.len() + 1];
        while let Some(Reverse(id)) = queue.pop() {
            orders[scope(id)].push(id);
            for &consumer in &next[id.index()] {
                waiting[consumer.index()] -= 1;
                if waiting[consumer.index()] == 0 {
                    queue.push(Reverse(consumer));
                }
            }
        }
        orders
    }

    /// Fills each step's `release`, in the scope its value lives in: after the last step
    /// there that reads it, an expand step reading what its block reads. A collected node
    /// lives in the scope of its collects. Outer outputs are never released.
    fn release_in_scopes(
        &self,
        scopes: &mut [Vec<Step>],
        expands: &[NodeId],
        block_of: &[Option<BlockId>],
        reads: &[Vec<NodeId>],
        is_collected: &[bool],
        outputs: &[NodeId],
    ) {
        // Releases, each in the scope its value lives in.
        let count = self.nodes().len();
        let mut last_use: Vec<Option<(usize, usize)>> = vec![None; count];
        for (at, steps) in scopes.iter().enumerate() {
            let key = at.checked_sub(1).map(|block| expands[block]);
            for (position, step) in steps.iter().enumerate() {
                for &arg in &step.args {
                    // A collected node is gathered into the scope its collects are in.
                    let block = self.node(arg).block;
                    let home = if is_collected[arg.index()] {
                        block.and_then(|expand| self.node(expand).block)
                    } else {
                        block
                    };
                    if home == key {
                        last_use[arg.index()] = Some((at, position));
                    }
                }
                if let Some(block) = block_of[step.node.index()] {
                    for &read in &reads[block.index()] {
                        let place = self.read_place(read, step.node);
                        let lives_here = if place == read {
                            self.node(read).block == key && !is_collected[read.index()]
                        } else {
                            self.node(place).block == key
                        };
                        if lives_here {
                            last_use[read.index()] = Some((at, position));
                        }
                    }
                }
            }
        }
        for (index, place) in last_use.into_iter().enumerate() {
            let Some((at, position)) = place else {
                continue;
            };
            let id = NodeId(index_u32(index));
            if at == 0 && outputs.binary_search(&id).is_ok() {
                continue;
            }
            scopes[at][position].release.push(id);
        }
    }

    /// Per reached block, the outer values its nodes read and the nodes collects read, both
    /// sorted; and whether each node is collected.
    fn block_reads(
        &self,
        actions: &[Option<Action>],
        block_of: &[Option<BlockId>],
    ) -> (Vec<Vec<NodeId>>, Vec<Vec<NodeId>>, Vec<bool>) {
        let blocks = block_of.iter().flatten().count();
        let mut reads: Vec<Vec<NodeId>> = vec![Vec::new(); blocks];
        let mut collected: Vec<Vec<NodeId>> = vec![Vec::new(); blocks];
        let mut is_collected = vec![false; self.nodes().len()];
        for node in self.nodes() {
            if actions[node.id.index()] != Some(Action::Compute) {
                continue;
            }
            let path = self.block_path(node.id);
            for edge in &node.inputs {
                let source = self.node(edge.node);
                if edge.collect.is_some() {
                    if let Some(block) = source.block.and_then(|e| block_of[e.index()]) {
                        collected[block.index()].push(source.id);
                        is_collected[source.id.index()] = true;
                    }
                    // A consumer inside a block below the collected block's own scope, as
                    // when it collects a sibling block: each block between reads the
                    // gathered values as an outer value.
                    let scope = self.block_path(source.id).len().saturating_sub(1);
                    for &expand in path.get(scope..).unwrap_or_default() {
                        if let Some(block) = block_of[expand.index()] {
                            reads[block.index()].push(source.id);
                        }
                    }
                    continue;
                }
                // The blocks of the path below the source's own: for an expand, its own block
                // reads it as the item, so that one is left out too.
                let below = self.block_path(source.id).len() + usize::from(source.is_expand());
                for &expand in path.get(below..).unwrap_or_default() {
                    if let Some(block) = block_of[expand.index()] {
                        reads[block.index()].push(source.id);
                    }
                }
            }
        }
        for list in reads.iter_mut().chain(collected.iter_mut()) {
            list.sort_unstable();
            list.dedup();
        }
        (reads, collected, is_collected)
    }

    /// The node whose place stands for `read` when the block of `expand` reads it: `read`
    /// itself for a value, or the expand of `read`'s block for the gathered values of a
    /// sibling block, which live in the scope its collects are in.
    fn read_place(&self, read: NodeId, expand: NodeId) -> NodeId {
        match self.node(read).block {
            Some(block) if block != expand && !self.block_path(expand).contains(&block) => block,
            _ => read,
        }
    }

    /// The unit that stands for `id` in a scope `depth` levels deep that holds it: `id`
    /// itself, or the expand of the nested block holding it. `None` when `id` is outside
    /// the scope, which is then done before the scope runs.
    fn unit_in(&self, id: NodeId, depth: usize) -> Option<NodeId> {
        let path = self.block_path(id);
        match path.len().cmp(&depth) {
            std::cmp::Ordering::Less => None,
            std::cmp::Ordering::Equal => Some(id),
            std::cmp::Ordering::Greater => Some(path[depth]),
        }
    }

    /// A flag per node, set for `ids`.
    fn mask(&self, ids: &[NodeId]) -> Vec<bool> {
        let mut mask = vec![false; self.nodes().len()];
        for id in ids {
            mask[id.index()] = true;
        }
        mask
    }

    /// The action of every node the outputs need, by id; `None` for the rest.
    ///
    /// An iterative reverse walk from the outputs, which stops at overrides and inputs.
    ///
    /// # Errors
    ///
    /// `PlanError::MissingInputs` naming each input that is neither supplied nor overridden yet
    /// is requested or read by a required edge of a computed node, with whether it was
    /// requested and the computed nodes that read it so.
    fn plan_actions(&self, resolved: &Resolved) -> Result<Vec<Option<Action>>, PlanError> {
        let mut actions = vec![None; self.nodes().len()];
        let mut missing = BTreeSet::new();
        for &id in &resolved.outputs {
            let action = self.action_for(id, resolved);
            if action == Action::SkipOptionalInput {
                missing.insert(id);
            }
            actions[id.index()] = Some(action);
        }
        let mut work = resolved.outputs.clone();
        while let Some(id) = work.pop() {
            if actions[id.index()] != Some(Action::Compute) {
                continue;
            }
            for edge in &self.node(id).inputs {
                let input = edge.node;
                if actions[input.index()].is_none() {
                    actions[input.index()] = Some(self.action_for(input, resolved));
                    work.push(input);
                }
                if !edge.dependency.has_default()
                    && actions[input.index()] == Some(Action::SkipOptionalInput)
                {
                    missing.insert(input);
                }
            }
        }
        if missing.is_empty() {
            Ok(actions)
        } else {
            Err(self.missing_inputs(&missing, &actions, resolved))
        }
    }

    /// The `MissingInputs` error for `missing`: each input with whether it was requested, and
    /// the computed nodes of `actions` reading it by a required edge.
    ///
    /// Kept out of `plan_actions`, so the walk a successful plan takes stays as small as it
    /// was. Ids follow name order, so the inputs and their consumers come out sorted by name.
    #[cold]
    #[inline(never)]
    fn missing_inputs(
        &self,
        missing: &BTreeSet<NodeId>,
        actions: &[Option<Action>],
        resolved: &Resolved,
    ) -> PlanError {
        let consumers_of = |input: NodeId| {
            self.nodes()
                .iter()
                .enumerate()
                .filter(|&(index, node)| {
                    actions[index] == Some(Action::Compute)
                        && node
                            .inputs
                            .iter()
                            .any(|edge| edge.node == input && !edge.dependency.has_default())
                })
                .map(|(_, node)| node.name.clone())
                .collect()
        };
        PlanError::MissingInputs(
            missing
                .iter()
                .map(|&id| MissingInput {
                    name: self.node(id).name.clone(),
                    requested: resolved.outputs.contains(&id),
                    consumers: consumers_of(id),
                })
                .collect(),
        )
    }

    /// The action for a needed node; an input that is not supplied is taken to be optional.
    fn action_for(&self, id: NodeId, resolved: &Resolved) -> Action {
        if resolved.overrides[id.index()] {
            return Action::UseOverride;
        }
        match self.node(id).kind {
            NodeKind::Function(_) | NodeKind::Expand(_) => Action::Compute,
            NodeKind::Input if resolved.supplied[id.index()] => Action::UseInput,
            NodeKind::Input => Action::SkipOptionalInput,
        }
    }

    /// Kahn's algorithm over the planned nodes; the ready set pops the smallest id first.
    ///
    /// Only computed nodes wait, each for all of its inputs, which the walk planned too.
    fn topological_order(&self, actions: &[Option<Action>]) -> Vec<NodeId> {
        let mut waiting: Vec<usize> = self
            .nodes()
            .iter()
            .zip(actions)
            .map(|(node, &action)| {
                if action == Some(Action::Compute) {
                    node.inputs.len()
                } else {
                    0
                }
            })
            .collect();
        let mut ready: BinaryHeap<Reverse<NodeId>> = self
            .nodes()
            .iter()
            .filter(|node| actions[node.id.index()].is_some() && waiting[node.id.index()] == 0)
            .map(|node| Reverse(node.id))
            .collect();
        let mut order = Vec::new();
        while let Some(Reverse(id)) = ready.pop() {
            order.push(id);
            for &consumer in self.dependents(id) {
                if actions[consumer.index()] == Some(Action::Compute) {
                    waiting[consumer.index()] -= 1;
                    if waiting[consumer.index()] == 0 {
                        ready.push(Reverse(consumer));
                    }
                }
            }
        }
        order
    }

    fn build_steps(
        &self,
        order: &[NodeId],
        actions: &[Option<Action>],
        outputs: &[NodeId],
    ) -> Vec<Step> {
        let mut steps: Vec<Step> = order
            .iter()
            .filter_map(|&node| {
                let action = actions[node.index()]?;
                Some(Step {
                    node,
                    action,
                    args: self.passed_args(node, action, actions),
                    release: Vec::new(),
                })
            })
            .collect();
        // For each node passed as an argument, the position of the last step that reads it.
        let mut last_use = vec![None; self.nodes().len()];
        for (position, step) in steps.iter().enumerate() {
            for &arg in &step.args {
                last_use[arg.index()] = Some(position);
            }
        }
        for (node, position) in self.nodes().iter().zip(last_use) {
            if let Some(position) = position {
                if outputs.binary_search(&node.id).is_err() {
                    steps[position].release.push(node.id);
                }
            }
        }
        steps
    }

    /// The inputs of a computed node that actually receive a value, sorted by id.
    fn passed_args(&self, id: NodeId, action: Action, actions: &[Option<Action>]) -> Vec<NodeId> {
        if action != Action::Compute {
            return Vec::new();
        }
        let mut args: Vec<NodeId> = self
            .node(id)
            .inputs
            .iter()
            .map(|edge| edge.node)
            .filter(|input| actions[input.index()] != Some(Action::SkipOptionalInput))
            .collect();
        args.sort_unstable();
        args
    }
}

/// `index` as a `u32` id; a graph never holds more nodes than ids can count.
fn index_u32(index: usize) -> u32 {
    u32::try_from(index).unwrap_or(u32::MAX)
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use super::*;
    use crate::node::Dependency;
    use crate::testing::{names, node, request, spec, strings};

    /// a, b <- a, c <- a, d <- b, c; `x` is an input of `a`.
    fn diamond() -> Graph<()> {
        Graph::build(vec![
            node("a", &["x"]),
            node("b", &["a"]),
            node("c", &["a"]),
            node("d", &["b", "c"]),
        ])
        .unwrap()
    }

    fn missing(name: &str, requested: bool, consumers: &[&str]) -> MissingInput {
        MissingInput {
            name: name.to_owned(),
            requested,
            consumers: strings(consumers),
        }
    }

    fn step_summary(graph: &Graph<()>, plan: &Plan) -> Vec<(String, Action)> {
        plan.steps()
            .iter()
            .map(|s| (graph.node(s.node).name.clone(), s.action))
            .collect()
    }

    #[test]
    fn unknown_output_is_refused() {
        let g = diamond();
        let err = g
            .prepare(&request(&["zzz", "d", "nope", "zzz"], &["x"], &[]))
            .unwrap_err();
        assert_eq!(err, PlanError::UnknownOutputs(strings(&["nope", "zzz"])));
    }

    #[test]
    fn unknown_input_name_is_refused() {
        let g = diamond();
        // `b` is a function node, `ghost` does not exist: both are unknown inputs (Q4).
        let err = g
            .prepare(&request(&["d"], &["x", "b", "ghost"], &[]))
            .unwrap_err();
        assert_eq!(err, PlanError::UnknownInputs(strings(&["b", "ghost"])));
    }

    #[test]
    fn unknown_override_name_is_refused() {
        let g = diamond();
        let err = g
            .prepare(&request(&["d"], &["x"], &["zzz", "b", "nope"]))
            .unwrap_err();
        assert_eq!(err, PlanError::UnknownOverrides(strings(&["nope", "zzz"])));
    }

    #[test]
    fn validation_reports_the_first_non_empty_category() {
        let g = diamond();
        let err = g
            .prepare(&request(&["nope"], &["ghost"], &["zzz"]))
            .unwrap_err();
        assert!(matches!(err, PlanError::UnknownOutputs(_)));
        let err = g
            .prepare(&request(&["d"], &["ghost"], &["zzz"]))
            .unwrap_err();
        assert!(matches!(err, PlanError::UnknownInputs(_)));
        let err = g.prepare(&request(&["d"], &["x"], &["zzz"])).unwrap_err();
        assert!(matches!(err, PlanError::UnknownOverrides(_)));
        let err = g.prepare(&request(&["d"], &[], &[])).unwrap_err();
        assert!(matches!(err, PlanError::MissingInputs(_)));
    }

    #[test]
    fn missing_required_input_lists_all_missing_sorted() {
        let g = Graph::build(vec![node("out", &["z", "m", "a"]), node("a", &["q"])]).unwrap();
        let err = g.prepare(&request(&["out"], &["m"], &[])).unwrap_err();
        assert_eq!(
            err,
            PlanError::MissingInputs(vec![
                missing("q", false, &["a"]),
                missing("z", false, &["out"]),
            ])
        );
    }

    #[test]
    fn a_missing_input_names_every_planned_consumer_sorted() {
        let g = Graph::build(vec![
            node("c", &["x"]),
            node("a", &["x"]),
            node("b", &["x"]),
            node("unplanned", &["x"]),
        ])
        .unwrap();
        let err = g.prepare(&request(&["c", "a", "x"], &[], &[])).unwrap_err();
        assert_eq!(
            err,
            PlanError::MissingInputs(vec![missing("x", true, &["a", "c"])])
        );
        assert_eq!(
            err.to_string(),
            "missing inputs: 'x' (requested as an output, needed by 'a', 'c')"
        );
    }

    #[test]
    fn missing_inputs_are_listed_with_their_reasons() {
        let g = Graph::build(vec![node("out", &["z", "q"])]).unwrap();
        let err = g.prepare(&request(&["out", "q"], &[], &[])).unwrap_err();
        assert_eq!(
            err.to_string(),
            "missing inputs: 'q' (requested as an output, needed by 'out'); \
             'z' (needed by 'out')"
        );
    }

    #[test]
    fn override_cuts_its_upstream() {
        let g = diamond();
        let plan = g.prepare(&request(&["d"], &[], &["b", "c"])).unwrap();
        assert_eq!(
            step_summary(&g, &plan),
            [
                ("b".to_owned(), Action::UseOverride),
                ("c".to_owned(), Action::UseOverride),
                ("d".to_owned(), Action::Compute),
            ]
        );
    }

    #[test]
    fn override_satisfies_a_missing_input_and_beats_a_supplied_one() {
        let g = diamond();
        let plan = g.prepare(&request(&["a"], &[], &["x"])).unwrap();
        assert_eq!(
            step_summary(&g, &plan),
            [
                ("x".to_owned(), Action::UseOverride),
                ("a".to_owned(), Action::Compute),
            ]
        );
        let plan = g.prepare(&request(&["a"], &["x"], &["x"])).unwrap();
        assert_eq!(plan.steps().first().unwrap().action, Action::UseOverride);
    }

    #[test]
    fn optional_input_not_supplied_is_skipped_and_omitted_from_args() {
        let g = Graph::build(vec![spec(
            "out",
            &[
                ("need", Dependency::Required),
                ("opt", Dependency::Optional),
            ],
        )])
        .unwrap();
        let plan = g.prepare(&request(&["out"], &["need"], &[])).unwrap();
        assert_eq!(
            step_summary(&g, &plan),
            [
                ("need".to_owned(), Action::UseInput),
                ("opt".to_owned(), Action::SkipOptionalInput),
                ("out".to_owned(), Action::Compute),
            ]
        );
        let out = plan.steps().last().unwrap();
        assert_eq!(names(&g, &out.args), ["need"]);

        let plan = g
            .prepare(&request(&["out"], &["need", "opt"], &[]))
            .unwrap();
        assert_eq!(
            names(&g, &plan.steps().last().unwrap().args),
            ["need", "opt"]
        );
    }

    #[test]
    fn an_input_required_by_any_planned_edge_is_required() {
        let g = Graph::build(vec![
            spec("p", &[("i", Dependency::Optional)]),
            spec("q", &[("i", Dependency::Required)]),
        ])
        .unwrap();
        assert!(g.prepare(&request(&["p"], &[], &[])).is_ok());
        let err = g.prepare(&request(&["p", "q"], &[], &[])).unwrap_err();
        assert_eq!(
            err,
            PlanError::MissingInputs(vec![missing("i", false, &["q"])])
        );
    }

    #[test]
    fn requested_unsupplied_input_is_missing() {
        let g = diamond();
        let err = g.prepare(&request(&["x"], &[], &[])).unwrap_err();
        assert_eq!(err, PlanError::MissingInputs(vec![missing("x", true, &[])]));
        assert_eq!(
            err.to_string(),
            "missing inputs: 'x' (requested as an output)"
        );
        let plan = g.prepare(&request(&["x"], &["x"], &[])).unwrap();
        assert_eq!(
            step_summary(&g, &plan),
            [("x".to_owned(), Action::UseInput)]
        );
    }

    #[test]
    fn empty_outputs_give_an_empty_plan() {
        let g = diamond();
        let plan = g.prepare(&request(&[], &[], &[])).unwrap();
        assert!(plan.steps().is_empty() && plan.outputs().is_empty());
        let err = g.prepare(&request(&[], &["ghost"], &[])).unwrap_err();
        assert!(matches!(err, PlanError::UnknownInputs(_)));
    }

    #[test]
    fn supplying_an_input_nothing_needs_is_valid() {
        let g = Graph::build(vec![node("a", &["x"]), node("b", &["y"])]).unwrap();
        let plan = g.prepare(&request(&["a"], &["x", "y"], &[])).unwrap();
        assert_eq!(
            step_summary(&g, &plan),
            [
                ("x".to_owned(), Action::UseInput),
                ("a".to_owned(), Action::Compute),
            ]
        );
    }

    #[test]
    fn release_lists_intermediates_after_last_use_and_never_outputs() {
        let g = diamond();
        let plan = g.prepare(&request(&["d", "b"], &["x"], &[])).unwrap();
        let summary: Vec<(String, Vec<String>, Vec<String>)> = plan
            .steps()
            .iter()
            .map(|s| {
                (
                    g.node(s.node).name.clone(),
                    names(&g, &s.args),
                    names(&g, &s.release),
                )
            })
            .collect();
        let row = |n: &str, a: &[&str], r: &[&str]| (n.to_owned(), strings(a), strings(r));
        assert_eq!(
            summary,
            [
                row("x", &[], &[]),
                row("a", &["x"], &["x"]),
                row("b", &["a"], &[]),
                row("c", &["a"], &["a"]),
                row("d", &["b", "c"], &["c"]),
            ]
        );
        assert_eq!(names(&g, plan.outputs()), ["b", "d"]);
    }

    #[test]
    fn execution_order_is_independent_of_output_order() {
        let g = diamond();
        let one = g.prepare(&request(&["d", "b"], &["x"], &[])).unwrap();
        let two = g.prepare(&request(&["b", "d"], &["x"], &[])).unwrap();
        assert_eq!(one.steps(), two.steps());
        assert_eq!(one.outputs(), two.outputs());
    }

    #[test]
    fn duplicate_outputs_are_collapsed() {
        let g = diamond();
        let plan = g.prepare(&request(&["b", "b"], &["x"], &[])).unwrap();
        assert_eq!(plan.outputs().len(), 1);
    }

    #[test]
    fn ties_are_broken_by_node_name() {
        let g = Graph::build(vec![node("z", &[]), node("m", &[]), node("a", &[])]).unwrap();
        let plan = g.prepare(&request(&["z", "m", "a"], &[], &[])).unwrap();
        assert_eq!(
            names(&g, &plan.steps().iter().map(|s| s.node).collect::<Vec<_>>()),
            ["a", "m", "z"]
        );
    }

    #[test]
    fn a_deep_chain_plans_without_recursion() {
        let specs: Vec<_> = (0..50_000)
            .map(|i| node(&format!("n{i:06}"), &[&format!("n{:06}", i + 1)]))
            .collect();
        let g = Graph::build(specs).unwrap();
        let plan = g
            .prepare(&request(&["n000000"], &["n050000"], &[]))
            .unwrap();
        assert_eq!(plan.steps().len(), 50_001);
    }

    mod blocks {
        //! Sub-plans: which blocks a request reaches, their order and their refusals.

        use super::*;
        use crate::error::OutputInBlock;
        use crate::node::NodeSpec;
        use crate::testing::{collect, expanding, plain, reading};

        /// `picked` expands; `details` reads each item and the outer `zz_rate`, which sorts
        /// after `picked`; `total` and `count` collect `details`.
        fn fan() -> Graph<()> {
            Graph::build(vec![
                expanding("picked", &[plain("wanted")], ()),
                reading("zz_rate", &[], ()),
                reading("details", &[plain("picked"), plain("zz_rate")], ()),
                reading("total", &[collect("details")], ()),
                reading("count", &[collect("details")], ()),
            ])
            .unwrap()
        }

        fn order(graph: &Graph<()>, steps: &[Step]) -> Vec<String> {
            steps
                .iter()
                .map(|s| graph.node(s.node).name.clone())
                .collect()
        }

        #[test]
        fn an_outer_value_read_only_by_the_block_and_named_after_the_expand_runs_first() {
            let g = fan();
            let plan = g.prepare(&request(&["total"], &["wanted"], &[])).unwrap();
            assert_eq!(
                order(&g, plan.steps()),
                ["wanted", "zz_rate", "picked", "total"]
            );
            assert_eq!(plan.blocks().len(), 1);
            let block = &plan.blocks()[0];
            assert_eq!(g.node(block.expand).name, "picked");
            assert_eq!(order(&g, &block.steps), ["details"]);
            assert_eq!(names(&g, &block.collected), ["details"]);
            assert_eq!(names(&g, &block.reads), ["zz_rate"]);
            let picked = &plan.steps()[2];
            assert_eq!(plan.block_of(picked.node), Some(BlockId(0)));
            // The outer value is released after the expand step, its last use.
            assert_eq!(names(&g, &picked.release), ["wanted", "zz_rate"]);
            // A collect's collected node is released after the last collect reading it.
            let total = &plan.steps()[3];
            assert_eq!(names(&g, &total.args), ["details"]);
            assert_eq!(names(&g, &total.release), ["details"]);
            assert!(plan.is_late(total.node) && !plan.is_late(picked.node));
            assert!(plan.is_late(block.steps[0].node));
            assert_eq!(block.steps[0].release, []);
            assert!(plan.has_expands());
        }

        #[test]
        fn a_nested_blocks_outer_read_named_after_its_expand_runs_first() {
            let g = Graph::build(vec![
                expanding("a", &[], ()),
                reading("zz", &[plain("a")], ()),
                expanding("b", &[plain("a")], ()),
                reading("leaf", &[plain("b"), plain("zz")], ()),
                reading("bs", &[collect("leaf")], ()),
                reading("total", &[collect("bs")], ()),
            ])
            .unwrap();
            let plan = g.prepare(&request(&["total"], &[], &[])).unwrap();
            assert_eq!(order(&g, plan.steps()), ["a", "total"]);
            let outer = &plan.blocks()[plan.block_of(plan.steps()[0].node).unwrap().index()];
            assert_eq!(order(&g, &outer.steps), ["zz", "b", "bs"]);
            assert_eq!(names(&g, &outer.collected), ["bs"]);
            let inner = &plan.blocks()[plan.block_of(outer.steps[1].node).unwrap().index()];
            assert_eq!(order(&g, &inner.steps), ["leaf"]);
            assert_eq!(names(&g, &inner.reads), ["zz"]);
            assert_eq!(names(&g, &outer.reads), [] as [&str; 0]);
            // `zz` is released in each outer branch once the inner block has run.
            assert_eq!(names(&g, &outer.steps[1].release), ["zz"]);
            assert_eq!(names(&g, &outer.steps[2].release), ["leaf"]);
        }

        #[test]
        fn a_collect_runs_after_its_block() {
            // Two collects, one of them reading another collect and an outer node: no step
            // is left waiting.
            let g = Graph::build(vec![
                expanding("e", &[plain("seed")], ()),
                reading("m", &[plain("e"), plain("seed")], ()),
                reading("n", &[plain("m")], ()),
                reading("c1", &[collect("m"), plain("seed")], ()),
                reading("c2", &[collect("n"), plain("c1")], ()),
            ])
            .unwrap();
            let plan = g.prepare(&request(&["c2"], &["seed"], &[])).unwrap();
            assert_eq!(order(&g, plan.steps()), ["seed", "e", "c1", "c2"]);
            assert_eq!(order(&g, &plan.blocks()[0].steps), ["m", "n"]);
            assert_eq!(names(&g, &plan.blocks()[0].collected), ["m", "n"]);
        }

        #[test]
        fn an_expand_requested_alone_compiles_no_sub_plan() {
            let g = fan();
            let plan = g.prepare(&request(&["picked"], &["wanted"], &[])).unwrap();
            assert_eq!(order(&g, plan.steps()), ["wanted", "picked"]);
            assert_eq!(plan.blocks(), []);
            assert_eq!(plan.block_of(plan.steps()[1].node), None);
            assert!(plan.has_expands());
        }

        #[test]
        fn a_collect_override_cuts_the_block() {
            let g = fan();
            let plan = g.prepare(&request(&["total"], &[], &["total"])).unwrap();
            assert_eq!(order(&g, plan.steps()), ["total"]);
            assert!(plan.blocks().is_empty() && !plan.has_expands());
        }

        #[test]
        fn an_output_in_a_block_is_refused_naming_its_collects() {
            let mut specs: Vec<NodeSpec<()>> = Vec::new();
            specs.extend([
                expanding("picked", &[plain("wanted")], ()),
                reading("zz_rate", &[], ()),
                reading("details", &[plain("picked"), plain("zz_rate")], ()),
                reading("total", &[collect("details")], ()),
                reading("count", &[collect("details")], ()),
                reading("lonely", &[plain("picked")], ()),
            ]);
            let g = Graph::build(specs).unwrap();
            let err = g
                .prepare(&request(
                    &["total", "lonely", "details"],
                    &["wanted"],
                    &["ghost"],
                ))
                .unwrap_err();
            assert!(matches!(err, PlanError::UnknownOverrides(_)));
            let err = g
                .prepare(&request(&["total", "lonely", "details"], &[], &[]))
                .unwrap_err();
            assert_eq!(
                err,
                PlanError::OutputsInBlocks(vec![
                    OutputInBlock {
                        node: "details".to_owned(),
                        expand: "picked".to_owned(),
                        collects: strings(&["count", "total"]),
                    },
                    OutputInBlock {
                        node: "lonely".to_owned(),
                        expand: "picked".to_owned(),
                        collects: Vec::new(),
                    },
                ])
            );
            assert_eq!(
                err.to_string(),
                "outputs inside blocks, with one value per branch: 'details' (in the block of \
                 'picked'; collected by 'count', 'total'); 'lonely' (in the block of 'picked'; \
                 collect it to return it)"
            );
        }

        #[test]
        fn an_override_of_a_block_node_is_refused() {
            let g = fan();
            // Checked before missing inputs: `wanted` is not supplied.
            let err = g
                .prepare(&request(&["total"], &[], &["details", "picked"]))
                .unwrap_err();
            assert_eq!(err, PlanError::OverridesInBlocks(strings(&["details"])));
            assert_eq!(
                err.to_string(),
                "overrides of nodes inside blocks, with one value per branch: 'details'"
            );
            // An override of the expand supplies its items.
            assert!(g.prepare(&request(&["total"], &[], &["picked"])).is_ok());
        }

        #[test]
        fn a_plan_without_blocks_or_absence_runs_the_plain_loop() {
            // A graph with a block, and a request that doesn't reach it.
            let g = fan();
            let plan = g.prepare(&request(&["zz_rate"], &[], &[])).unwrap();
            assert!(!plan.has_expands() && plan.blocks().is_empty() && !plan.needs_rules());
            assert!(
                plan.steps()
                    .iter()
                    .all(|s| plan.block_of(s.node).is_none() && !plan.is_late(s.node))
            );
        }

        #[test]
        fn a_step_is_no_larger_than_without_blocks() {
            // A larger `Step` moves the allocator's layout under the plain loop, which the
            // instruction-count gate sees on `run.fan_in_1000`.
            assert!(std::mem::size_of::<Step>() <= 56);
        }

        #[test]
        fn a_thousand_deep_nesting_plans_without_recursion() {
            let depth = 1_000;
            let mut specs = vec![expanding("e0000", &[], ())];
            specs.extend(
                (1..depth).map(|i| {
                    expanding(&format!("e{i:04}"), &[plain(&format!("e{:04}", i - 1))], ())
                }),
            );
            specs.push(reading("leaf", &[plain(&format!("e{:04}", depth - 1))], ()));
            specs.push(reading(
                &format!("c{:04}", depth - 1),
                &[collect("leaf")],
                (),
            ));
            specs.extend((0..depth - 1).map(|i| {
                reading(
                    &format!("c{i:04}"),
                    &[collect(&format!("c{:04}", i + 1))],
                    (),
                )
            }));
            let g = Graph::build(specs).unwrap();
            let plan = g.prepare(&request(&["c0000"], &[], &[])).unwrap();
            assert_eq!(plan.blocks().len(), depth);
        }
    }
}
