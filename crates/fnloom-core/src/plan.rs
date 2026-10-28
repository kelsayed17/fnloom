//! Turning a request into a validated, reusable execution plan.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};

use crate::error::{MissingInput, PlanError};
use crate::graph::Graph;
use crate::node::{Dependency, Node, NodeId, NodeKind};

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
    /// `MissingInputs`, each listing its names sorted and de-duplicated.
    pub fn prepare(&self, request: &Request) -> Result<Plan, PlanError> {
        let resolved = self.resolve_request(request)?;
        let actions = self.plan_actions(&resolved)?;
        let order = self.topological_order(&actions);
        Ok(Plan {
            steps: self.build_steps(&order, &actions, &resolved.outputs),
            outputs: resolved.outputs,
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
        // Each missing input: whether it was requested, and its required readers. Ids follow
        // name order, so both come out sorted by name.
        let mut missing: BTreeMap<NodeId, (bool, BTreeSet<NodeId>)> = BTreeMap::new();
        for &id in &resolved.outputs {
            let action = self.action_for(id, resolved);
            if action == Action::SkipOptionalInput {
                missing.entry(id).or_default().0 = true;
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
                if edge.dependency == Dependency::Required
                    && actions[input.index()] == Some(Action::SkipOptionalInput)
                {
                    missing.entry(input).or_default().1.insert(id);
                }
            }
        }
        if missing.is_empty() {
            Ok(actions)
        } else {
            Err(PlanError::MissingInputs(
                missing
                    .into_iter()
                    .map(|(id, (requested, consumers))| MissingInput {
                        name: self.node(id).name.clone(),
                        requested,
                        consumers: consumers
                            .into_iter()
                            .map(|consumer| self.node(consumer).name.clone())
                            .collect(),
                    })
                    .collect(),
            ))
        }
    }

    /// The action for a needed node; an input that is not supplied is taken to be optional.
    fn action_for(&self, id: NodeId, resolved: &Resolved) -> Action {
        if resolved.overrides[id.index()] {
            return Action::UseOverride;
        }
        match self.node(id).kind {
            NodeKind::Function(_) => Action::Compute,
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

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use super::*;
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
}
