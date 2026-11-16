//! Fixtures shared by the unit tests.

use std::collections::BTreeMap;

use crate::graph::Graph;
use crate::node::{Collect, Dependency, InputSpec, NodeId, NodeSpec};
use crate::plan::Request;

/// A spec called `name` with `payload`, reading each `(input, dependency)` pair.
pub fn valued_spec<P>(name: &str, deps: &[(&str, Dependency)], payload: P) -> NodeSpec<P> {
    NodeSpec {
        name: name.to_owned(),
        inputs: deps
            .iter()
            .map(|&(input, dependency)| InputSpec {
                name: input.to_owned(),
                dependency,
                collect: None,
            })
            .collect(),
        tags: BTreeMap::new(),
        payload,
        declines: false,
        expands: false,
        absent_on_error: false,
    }
}

/// A spec like `valued_spec` whose node may decline: its value may be absent.
pub fn declining<P>(name: &str, deps: &[(&str, Dependency)], payload: P) -> NodeSpec<P> {
    NodeSpec {
        declines: true,
        expands: false,
        ..valued_spec(name, deps, payload)
    }
}

/// A spec called `name` reading each `(input, dependency)` pair.
pub fn spec(name: &str, deps: &[(&str, Dependency)]) -> NodeSpec<()> {
    valued_spec(name, deps, ())
}

/// A spec called `name` with `payload` that requires every one of `deps`.
pub fn valued<P>(name: &str, deps: &[&str], payload: P) -> NodeSpec<P> {
    let required: Vec<_> = deps.iter().map(|&d| (d, Dependency::Required)).collect();
    valued_spec(name, &required, payload)
}

/// A spec called `name` that requires every one of `deps`.
pub fn node(name: &str, deps: &[&str]) -> NodeSpec<()> {
    valued(name, deps, ())
}

/// Owned copies of `names`.
pub fn strings(names: &[&str]) -> Vec<String> {
    names.iter().map(|&name| name.to_owned()).collect()
}

/// A request for `outputs`, supplying `inputs` and overriding `overrides`.
pub fn request(outputs: &[&str], inputs: &[&str], overrides: &[&str]) -> Request {
    Request {
        outputs: strings(outputs),
        inputs: strings(inputs).into_iter().collect(),
        overrides: strings(overrides).into_iter().collect(),
    }
}

/// The names of `ids` in `graph`.
pub fn names<P>(graph: &Graph<P>, ids: &[NodeId]) -> Vec<String> {
    ids.iter().map(|&id| graph.node(id).name.clone()).collect()
}

/// How a test spec reads one input: its name, its dependency, and whether it collects.
pub type Read<'a> = (&'a str, Dependency, Option<Collect>);

/// A required, ordinary read of `name`.
pub const fn plain(name: &str) -> Read<'_> {
    (name, Dependency::Required, None)
}

/// A required collect of `name` that keeps the present branches.
pub const fn collect(name: &str) -> Read<'_> {
    (name, Dependency::Required, Some(Collect::Present))
}

/// A spec called `name` with `payload`, reading each of `reads`.
pub fn reading<P>(name: &str, reads: &[Read<'_>], payload: P) -> NodeSpec<P> {
    NodeSpec {
        inputs: reads
            .iter()
            .map(|&(input, dependency, collect)| InputSpec {
                name: input.to_owned(),
                dependency,
                collect,
            })
            .collect(),
        ..valued_spec(name, &[], payload)
    }
}

/// A spec like `reading` whose node is an expand.
pub fn expanding<P>(name: &str, reads: &[Read<'_>], payload: P) -> NodeSpec<P> {
    NodeSpec {
        expands: true,
        ..reading(name, reads, payload)
    }
}

/// Blocks run by a recording evaluator, shared by `Plan::run`'s tests and the scheduler's.
#[expect(
    clippy::unwrap_used,
    reason = "a fixture that does not build is a bug in the test"
)]
pub mod blocks {
    use std::collections::BTreeMap;
    use std::rc::Rc;

    use super::{Read, collect, expanding, plain, reading};
    use crate::graph::Graph;
    use crate::node::{Collect, Dependency, Node, NodeId, NodeSpec};
    use crate::run::{Arg, Blocks, Evaluate, Item, Observer};

    /// How a test iterable ends once its items are pulled.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum End {
        Done,
        Decline,
        Fail,
    }

    /// The values of these tests. `Mark` counts live values through its `Rc`.
    #[derive(Clone, Debug, PartialEq)]
    pub enum Val {
        Num(i64),
        List(Vec<Val>),
        Gen(Vec<Val>, End),
        Iter(usize),
        Absent,
        Mark(i64, Rc<()>),
    }

    use Val::{Absent, List, Num};

    pub fn nums(values: &[i64]) -> Val {
        List(values.iter().map(|&n| Num(n)).collect())
    }

    pub fn items(values: &[i64]) -> Val {
        Val::Gen(values.iter().map(|&n| Num(n)).collect(), End::Done)
    }

    pub fn num(value: &Val) -> i64 {
        match value {
            Num(n) | Val::Mark(n, _) => *n,
            List(values) => values.iter().map(num).sum(),
            _ => 0,
        }
    }

    pub type Pay = Rc<dyn Fn(&[Val]) -> Result<Val, String>>;

    pub fn node(
        name: &str,
        reads: &[Read<'_>],
        body: impl Fn(&[Val]) -> Result<Val, String> + 'static,
    ) -> NodeSpec<Pay> {
        reading(name, reads, Rc::new(body) as Pay)
    }

    pub fn expand(
        name: &str,
        reads: &[Read<'_>],
        body: impl Fn(&[Val]) -> Result<Val, String> + 'static,
    ) -> NodeSpec<Pay> {
        expanding(name, reads, Rc::new(body) as Pay)
    }

    pub fn declines(spec: NodeSpec<Pay>) -> NodeSpec<Pay> {
        NodeSpec {
            declines: true,
            ..spec
        }
    }

    /// `spec`, absent on an error: `Fan` makes every error of its absent, unless it
    /// `refuses`.
    pub fn absent_on(spec: NodeSpec<Pay>) -> NodeSpec<Pay> {
        NodeSpec {
            absent_on_error: true,
            ..spec
        }
    }

    /// The sum of the arguments, times ten.
    #[expect(clippy::unnecessary_wraps, reason = "a payload returns a `Result`")]
    pub fn tens(args: &[Val]) -> Result<Val, String> {
        Ok(Num(10 * args.iter().map(num).sum::<i64>()))
    }

    /// The arguments as a list, one per argument.
    #[expect(clippy::unnecessary_wraps, reason = "a payload returns a `Result`")]
    pub fn echo(args: &[Val]) -> Result<Val, String> {
        Ok(List(args.to_vec()))
    }

    /// The single argument, unchanged.
    #[expect(clippy::unnecessary_wraps, reason = "a payload returns a `Result`")]
    pub fn first(args: &[Val]) -> Result<Val, String> {
        Ok(args.first().cloned().unwrap_or(List(Vec::new())))
    }

    /// One open iterator: its items, how it ends, and how many it gave.
    pub struct Open {
        pub items: Vec<Val>,
        pub end: End,
        pub given: usize,
    }

    /// Runs payloads and blocks, recording everything it is asked.
    #[derive(Default)]
    pub struct Fan {
        pub calls: Vec<(String, Vec<usize>)>,
        pub open: Vec<Open>,
        pub finished: Vec<(String, bool)>,
        pub asked: Vec<(String, Vec<usize>)>,
        pub hits: BTreeMap<(String, Vec<usize>), Val>,
        pub fail: Option<(&'static str, Vec<usize>)>,
        pub lookup_fails: Option<(&'static str, Vec<usize>)>,
        pub iterated: Vec<(String, Vec<usize>)>,
        /// Single-flight keys by node and branch, which a probe gives the scheduler.
        pub keys: BTreeMap<(String, Vec<usize>), String>,
        /// Whether `absent_on_error` refuses every error, as an error not listed.
        pub refuses: bool,
    }

    impl Fan {
        pub fn called(&self, name: &str) -> Vec<Vec<usize>> {
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

        fn absent_on_error(&mut self, _node: &Node<Pay>, _error: &String) -> Option<Val> {
            (!self.refuses).then_some(Absent)
        }

        fn lookup(&mut self, node: &Node<Pay>, branch: &[usize]) -> Result<Option<Val>, String> {
            self.asked.push((node.name.clone(), branch.to_vec()));
            if self
                .lookup_fails
                .as_ref()
                .is_some_and(|(n, b)| *n == node.name && b == branch)
            {
                return Err(format!("lookup of {} failed", node.name));
            }
            Ok(self
                .hits
                .get(&(node.name.clone(), branch.to_vec()))
                .cloned())
        }
    }

    impl Blocks<Pay, Val, String> for Fan {
        fn iterate(
            &mut self,
            node: &Node<Pay>,
            iterable: &Val,
            branch: &[usize],
        ) -> Result<Val, String> {
            self.iterated.push((node.name.clone(), branch.to_vec()));
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

    /// Logs every event with its branch; fails the events that start with `fail_on`.
    #[derive(Default)]
    pub struct Log {
        pub events: Vec<String>,
        pub fail_on: Option<&'static str>,
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
            let event = format!("expand:{node}:{branch:?}:{items}");
            self.events.push(event.clone());
            match self.fail_on {
                Some(prefix) if event.starts_with(prefix) => Err(event),
                _ => Ok(()),
            }
        }
        fn hit_in(&mut self, node: &str, branch: &[usize], _value: &Val) -> Result<(), String> {
            self.events.push(format!("hit:{node}:{branch:?}"));
            Ok(())
        }
        fn skipped(&mut self, node: &str, branch: &[usize]) -> Result<(), String> {
            self.events.push(format!("skipped:{node}:{branch:?}"));
            Ok(())
        }
        fn absent_error(
            &mut self,
            node: &str,
            branch: &[usize],
            error: &String,
        ) -> Result<(), String> {
            let event = format!("absent:{node}:{branch:?}:{error}");
            self.events.push(event.clone());
            match self.fail_on {
                Some(prefix) if event.starts_with(prefix) => Err(event),
                _ => Ok(()),
            }
        }
    }

    /// `picked` yields `of`; `details` is ten times each item plus the shared `zz_rate`;
    /// `total` collects `details`.
    pub fn fan(of: Val) -> Graph<Pay> {
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

    /// `outer` yields `[2, 1]`; `inner` yields `0..k` for each outer item `k`; `leaf` is
    /// `10 * outer + inner`; `inner_sum` collects `leaf` per outer branch, `outer_sum`
    /// collects `inner_sum`.
    pub fn nested() -> Graph<Pay> {
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

    /// `details` declines for the item 2; `total` keeps the present branches, `every`
    /// every branch.
    pub fn declining_details() -> Graph<Pay> {
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

    /// `picked` yields `of` and may decline; `req`, `opt` and `adm` collect `details` with
    /// each of the three dependencies.
    pub fn absent_expand(of: Val) -> Graph<Pay> {
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

    /// `a` reads each item, `b` reads `a`, `c` reads `a`; `total` collects `b` and `count`
    /// collects `c`.
    pub fn chain() -> Graph<Pay> {
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

    pub fn hit(name: &str, branch: &[usize], value: Val) -> ((String, Vec<usize>), Val) {
        ((name.to_owned(), branch.to_vec()), value)
    }

    pub fn asked(pairs: &[(&str, &[usize])]) -> Vec<(String, Vec<usize>)> {
        pairs
            .iter()
            .map(|&(n, b)| (n.to_owned(), b.to_vec()))
            .collect()
    }

    /// `zb` yields `[1, 2]` and `zb_m` is ten times each; inside the block of `ca`, `n`
    /// collects the sibling block of `zb`; `total` collects `n`. Named so that `ca`'s block
    /// sorts before `zb`'s, which it must still wait for.
    pub fn sibling_collect(inside: Option<&'static str>) -> Vec<NodeSpec<Pay>> {
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
}
