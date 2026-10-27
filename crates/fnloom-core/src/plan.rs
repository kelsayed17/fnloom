//! Turning a request into a validated, reusable execution plan.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};

use crate::error::PlanError;
use crate::graph::Graph;
use crate::node::{Dependency, NodeId, NodeKind};

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

/// A validated execution order for one request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    steps: Vec<Step>,
    outputs: Vec<NodeId>,
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
}

/// A request whose names have been resolved to ids.
struct Resolved {
    outputs: BTreeSet<NodeId>,
    supplied: BTreeSet<NodeId>,
    overrides: BTreeSet<NodeId>,
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
    /// `MissingInputs`, each listing its names sorted and de-duplicated.
    pub fn prepare(&self, request: &Request) -> Result<Plan, PlanError> {
        let resolved = self.resolve_request(request)?;
        let (needed, required) = self.walk(&resolved);
        self.check_missing(&resolved, &required)?;
        let actions: BTreeMap<NodeId, Action> = needed
            .iter()
            .map(|&id| (id, self.action_for(id, &resolved)))
            .collect();
        let order = self.topological_order(&actions);
        Ok(Plan {
            steps: self.build_steps(&order, &actions, &resolved.outputs),
            outputs: resolved.outputs.into_iter().collect(),
        })
    }

    fn resolve_request(&self, request: &Request) -> Result<Resolved, PlanError> {
        let outputs = self.lookup(request.outputs.iter(), PlanError::UnknownOutputs, |_| true)?;
        let supplied = self.lookup(request.inputs.iter(), PlanError::UnknownInputs, |kind| {
            kind == NodeKind::Input
        })?;
        let overrides = self.lookup(
            request.overrides.iter(),
            PlanError::UnknownOverrides,
            |_| true,
        )?;
        Ok(Resolved {
            outputs,
            supplied,
            overrides,
        })
    }

    /// Ids of the names whose node passes `accept`, or the offending names via `error`.
    fn lookup<'a>(
        &self,
        names: impl Iterator<Item = &'a String>,
        error: fn(Vec<String>) -> PlanError,
        accept: fn(NodeKind) -> bool,
    ) -> Result<BTreeSet<NodeId>, PlanError> {
        let mut found = BTreeSet::new();
        let mut rejected = BTreeSet::new();
        for name in names {
            match self.id_of(name).filter(|&id| accept(self.node(id).kind)) {
                Some(id) => found.insert(id),
                None => rejected.insert(name.clone()),
            };
        }
        if rejected.is_empty() {
            Ok(found)
        } else {
            Err(error(rejected.into_iter().collect()))
        }
    }

    /// Iterative reverse walk from the outputs.
    ///
    /// Returns the needed nodes and the `Input` nodes some planned edge requires.
    fn walk(&self, resolved: &Resolved) -> (BTreeSet<NodeId>, BTreeSet<NodeId>) {
        let mut needed: BTreeSet<NodeId> = resolved.outputs.clone();
        let mut required: BTreeSet<NodeId> = resolved
            .outputs
            .iter()
            .copied()
            .filter(|&id| self.node(id).kind == NodeKind::Input)
            .collect();
        let mut work: Vec<NodeId> = needed.iter().copied().collect();
        while let Some(id) = work.pop() {
            if resolved.overrides.contains(&id) {
                continue;
            }
            for edge in &self.node(id).inputs {
                if edge.dependency == Dependency::Required
                    && self.node(edge.node).kind == NodeKind::Input
                {
                    required.insert(edge.node);
                }
                if needed.insert(edge.node) {
                    work.push(edge.node);
                }
            }
        }
        (needed, required)
    }

    fn check_missing(
        &self,
        resolved: &Resolved,
        required: &BTreeSet<NodeId>,
    ) -> Result<(), PlanError> {
        let missing: Vec<String> = required
            .iter()
            .filter(|id| !resolved.supplied.contains(id) && !resolved.overrides.contains(id))
            .map(|&id| self.node(id).name.clone())
            .collect();
        if missing.is_empty() {
            Ok(())
        } else {
            Err(PlanError::MissingInputs(missing))
        }
    }

    /// The action for a needed node; unsupplied inputs reaching here are optional ones.
    fn action_for(&self, id: NodeId, resolved: &Resolved) -> Action {
        if resolved.overrides.contains(&id) {
            return Action::UseOverride;
        }
        match self.node(id).kind {
            NodeKind::Function => Action::Compute,
            NodeKind::Input if resolved.supplied.contains(&id) => Action::UseInput,
            NodeKind::Input => Action::SkipOptionalInput,
        }
    }

    /// Kahn's algorithm over the planned nodes; the ready set pops the smallest id first.
    fn topological_order(&self, actions: &BTreeMap<NodeId, Action>) -> Vec<NodeId> {
        let mut waiting: BTreeMap<NodeId, usize> = BTreeMap::new();
        let mut consumers: BTreeMap<NodeId, Vec<NodeId>> = BTreeMap::new();
        for (&id, &action) in actions {
            let deps = self.planned_dependencies(id, action);
            waiting.insert(id, deps.len());
            for dep in deps {
                consumers.entry(dep).or_default().push(id);
            }
        }
        let mut ready: BinaryHeap<Reverse<NodeId>> = waiting
            .iter()
            .filter(|&(_, &count)| count == 0)
            .map(|(&id, _)| Reverse(id))
            .collect();
        let mut order = Vec::with_capacity(actions.len());
        while let Some(Reverse(id)) = ready.pop() {
            order.push(id);
            for &consumer in consumers.get(&id).into_iter().flatten() {
                if let Some(count) = waiting.get_mut(&consumer) {
                    *count -= 1;
                    if *count == 0 {
                        ready.push(Reverse(consumer));
                    }
                }
            }
        }
        order
    }

    /// Nodes that must be ordered before `id`: only computed nodes have any.
    fn planned_dependencies(&self, id: NodeId, action: Action) -> Vec<NodeId> {
        if action == Action::Compute {
            self.node(id).inputs.iter().map(|edge| edge.node).collect()
        } else {
            Vec::new()
        }
    }

    fn build_steps(
        &self,
        order: &[NodeId],
        actions: &BTreeMap<NodeId, Action>,
        outputs: &BTreeSet<NodeId>,
    ) -> Vec<Step> {
        let mut steps: Vec<Step> = order
            .iter()
            .map(|&node| {
                let action = actions.get(&node).copied().unwrap_or(Action::Compute);
                Step {
                    node,
                    action,
                    args: self.passed_args(node, action, actions),
                    release: Vec::new(),
                }
            })
            .collect();
        let last_use = last_use_positions(&steps);
        for (&node, &position) in &last_use {
            if !outputs.contains(&node) {
                if let Some(step) = steps.get_mut(position) {
                    step.release.push(node);
                }
            }
        }
        steps
    }

    /// Dependencies of a computed node that actually receive a value, sorted by id.
    fn passed_args(
        &self,
        id: NodeId,
        action: Action,
        actions: &BTreeMap<NodeId, Action>,
    ) -> Vec<NodeId> {
        let args: BTreeSet<NodeId> = self
            .planned_dependencies(id, action)
            .into_iter()
            .filter(|dep| actions.get(dep) != Some(&Action::SkipOptionalInput))
            .collect();
        args.into_iter().collect()
    }
}

/// For each node passed as an argument, the position of the last step that consumes it.
fn last_use_positions(steps: &[Step]) -> BTreeMap<NodeId, usize> {
    let mut last = BTreeMap::new();
    for (position, step) in steps.iter().enumerate() {
        for &arg in &step.args {
            last.insert(arg, position);
        }
    }
    last
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use super::*;
    use crate::node::{InputSpec, NodeSpec};

    fn spec(name: &str, deps: &[(&str, Dependency)]) -> NodeSpec<()> {
        NodeSpec {
            name: name.to_owned(),
            inputs: deps
                .iter()
                .map(|&(d, dependency)| InputSpec {
                    name: d.to_owned(),
                    dependency,
                })
                .collect(),
            tags: BTreeMap::new(),
            payload: (),
        }
    }

    fn node(name: &str, deps: &[&str]) -> NodeSpec<()> {
        let required: Vec<(&str, Dependency)> =
            deps.iter().map(|&d| (d, Dependency::Required)).collect();
        spec(name, &required)
    }

    fn request(outputs: &[&str], inputs: &[&str], overrides: &[&str]) -> Request {
        Request {
            outputs: outputs.iter().map(|s| (*s).to_owned()).collect(),
            inputs: inputs.iter().map(|s| (*s).to_owned()).collect(),
            overrides: overrides.iter().map(|s| (*s).to_owned()).collect(),
        }
    }

    fn strings(names: &[&str]) -> Vec<String> {
        names.iter().map(|s| (*s).to_owned()).collect()
    }

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

    fn names(graph: &Graph<()>, ids: &[NodeId]) -> Vec<String> {
        ids.iter().map(|&id| graph.node(id).name.clone()).collect()
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
        assert_eq!(err, PlanError::MissingInputs(strings(&["q", "z"])));
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
        assert_eq!(err, PlanError::MissingInputs(strings(&["i"])));
    }

    #[test]
    fn requested_unsupplied_input_is_missing() {
        let g = diamond();
        let err = g.prepare(&request(&["x"], &[], &[])).unwrap_err();
        assert_eq!(err, PlanError::MissingInputs(strings(&["x"])));
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
}
