//! How many tasks may run at once: the `Grant` a scheduler asks before it hands one out.

use std::sync::atomic::{AtomicUsize, Ordering};

use crate::node::NodeId;
use crate::schedule::Seat;

/// Grants the slots a task needs before `Scheduler::next` hands it out, and takes them back
/// when `Scheduler::complete` gets its outcome.
///
/// `node` is the node whose code the task runs, for a task that computes or pulls one, so a
/// grant can cap a node and its group; it is `None` for a task that runs none of a node's
/// own code: an iterate, a finish or a probe. `seat` says whether the task takes a worker
/// slot (`Seat`). A grant is shared by every run of a driver, so it is `Sync`.
pub trait Grant: Sync {
    /// Takes the task's slots and returns `true`, or takes none and returns `false`.
    fn try_start(&self, node: Option<NodeId>, seat: Seat) -> bool;

    /// Gives back the slots `try_start` took for a task with the same `node` and `seat`.
    fn release(&self, node: Option<NodeId>, seat: Seat);
}

/// The grant that refuses nothing: no worker, node or group cap.
#[derive(Clone, Copy, Debug, Default)]
pub struct Unlimited;

impl Grant for Unlimited {
    fn try_start(&self, _node: Option<NodeId>, _seat: Seat) -> bool {
        true
    }

    fn release(&self, _node: Option<NodeId>, _seat: Seat) {}
}

/// One kind of slot a task may hold, for `Limits::held`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Slot {
    /// A worker slot, of `max_workers`.
    Worker,
    /// A slot of a node's own cap.
    Node(NodeId),
    /// A slot of a group's cap.
    Group(u32),
}

/// A cap, and how many of its units are held.
#[derive(Debug)]
struct Cap {
    limit: usize,
    held: AtomicUsize,
}

impl Cap {
    const fn new(limit: usize) -> Self {
        Self {
            limit,
            held: AtomicUsize::new(0),
        }
    }

    /// Takes a unit if one is free.
    fn take(&self) -> bool {
        self.update(|held| (held < self.limit).then_some(held + 1))
    }

    /// Gives a unit back; never below zero.
    fn give(&self) {
        self.update(|held| held.checked_sub(1));
    }

    /// Moves the count by `step`, unless it gives `None`; whether it moved.
    // shortcut: a compare-exchange loop by hand, since `fetch_update` is deprecated on
    // current Rust and its replacement, `try_update`, is newer than the MSRV (1.85).
    fn update(&self, step: impl Fn(usize) -> Option<usize>) -> bool {
        let mut held = self.held.load(Ordering::Acquire);
        loop {
            let Some(next) = step(held) else {
                return false;
            };
            match self
                .held
                .compare_exchange_weak(held, next, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => return true,
                Err(now) => held = now,
            }
        }
    }
}

/// The caps of one driver, shared by every run's scheduler: workers, each node's own cap,
/// and each group's, as atomic counters only, so they hold across concurrent runs.
///
/// A task takes one worker slot when its seat is `Seat::Take`, and none when it is `Held`
/// (a run's caller, already holding one), `Lent` (the slot a waiting caller lent its run)
/// or `Threadless` (a task that holds no thread). A task that computes or pulls a node also
/// takes that node's slot and its group's. `Limits` has no wake: a caller that waits for a
/// slot adds its own, since the core has no locks.
#[derive(Debug)]
pub struct Limits {
    workers: Option<Cap>,
    nodes: Vec<Option<Cap>>,
    groups_of: Vec<Option<u32>>,
    groups: Vec<Cap>,
}

impl Limits {
    /// Caps of `max_workers` workers (`None`: no worker cap, a driver without threads), each
    /// node's own cap by `NodeId::index`, each node's group, and each group's size by id. A
    /// node past the tables, or in a group with no size, has no cap of that kind.
    #[must_use]
    pub fn new(
        max_workers: Option<usize>,
        node_caps: Vec<Option<usize>>,
        node_groups: Vec<Option<u32>>,
        group_caps: Vec<usize>,
    ) -> Self {
        Self {
            workers: max_workers.map(Cap::new),
            nodes: node_caps.into_iter().map(|cap| cap.map(Cap::new)).collect(),
            groups_of: node_groups,
            groups: group_caps.into_iter().map(Cap::new).collect(),
        }
    }

    /// How many units of `slot` are held now; 0 for a slot with no cap.
    #[must_use]
    pub fn held(&self, slot: Slot) -> usize {
        let cap = match slot {
            Slot::Worker => self.workers.as_ref(),
            Slot::Node(node) => self.node_cap(node),
            Slot::Group(group) => self.groups.get(group as usize),
        };
        cap.map_or(0, |cap| cap.held.load(Ordering::Acquire))
    }

    fn node_cap(&self, node: NodeId) -> Option<&Cap> {
        self.nodes.get(node.index()).and_then(Option::as_ref)
    }

    /// The caps a task of `node` on `seat` takes a unit of.
    fn caps(&self, node: Option<NodeId>, seat: Seat) -> [Option<&Cap>; 3] {
        let worker = if seat == Seat::Take {
            self.workers.as_ref()
        } else {
            None
        };
        let group = node
            .and_then(|node| self.groups_of.get(node.index()).copied().flatten())
            .and_then(|group| self.groups.get(group as usize));
        [worker, node.and_then(|node| self.node_cap(node)), group]
    }
}

impl Grant for Limits {
    fn try_start(&self, node: Option<NodeId>, seat: Seat) -> bool {
        let caps = self.caps(node, seat);
        for (at, cap) in caps.iter().enumerate() {
            if cap.is_some_and(|cap| !cap.take()) {
                caps[..at].iter().flatten().for_each(|cap| cap.give());
                return false;
            }
        }
        true
    }

    fn release(&self, node: Option<NodeId>, seat: Seat) {
        self.caps(node, seat)
            .iter()
            .flatten()
            .for_each(|cap| cap.give());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Two capped nodes: `n0` at 1, and `n1` and `n2` in group 0, of size 1.
    fn limits(max_workers: Option<usize>) -> Limits {
        Limits::new(
            max_workers,
            vec![Some(1), None, None],
            vec![None, Some(0), Some(0)],
            vec![1],
        )
    }

    const N0: Option<NodeId> = Some(NodeId(0));
    const N1: Option<NodeId> = Some(NodeId(1));
    const N2: Option<NodeId> = Some(NodeId(2));

    #[test]
    fn workers_nodes_and_groups_are_capped_and_given_back() {
        let limits = limits(Some(2));
        assert!(limits.try_start(N0, Seat::Take));
        // `n0` is at its cap; a refusal takes nothing.
        assert!(!limits.try_start(N0, Seat::Take));
        assert_eq!(limits.held(Slot::Worker), 1);
        assert!(limits.try_start(N1, Seat::Take));
        assert_eq!(limits.held(Slot::Group(0)), 1);
        // Two workers are out.
        assert!(!limits.try_start(None, Seat::Take));
        limits.release(N0, Seat::Take);
        assert_eq!(
            (
                limits.held(Slot::Worker),
                limits.held(Slot::Node(NodeId(0)))
            ),
            (1, 0)
        );
        // `n2` shares `n1`'s group.
        assert!(!limits.try_start(N2, Seat::Take));
        assert_eq!(limits.held(Slot::Worker), 1);
        limits.release(N1, Seat::Take);
        assert!(limits.try_start(N2, Seat::Take));
    }

    #[test]
    fn no_worker_cap_without_max_workers() {
        let limits = limits(None);
        for _ in 0..100 {
            assert!(limits.try_start(None, Seat::Take));
        }
        assert_eq!(limits.held(Slot::Worker), 0);
        // Node caps still hold.
        assert!(limits.try_start(N0, Seat::Take));
        assert!(!limits.try_start(N0, Seat::Take));
    }

    #[test]
    fn a_held_seat_takes_no_worker_slot() {
        let limits = limits(Some(1));
        assert!(limits.try_start(None, Seat::Take));
        assert!(!limits.try_start(None, Seat::Take));
        for seat in [Seat::Held, Seat::Lent] {
            assert!(limits.try_start(None, seat));
            limits.release(None, seat);
        }
        assert_eq!(limits.held(Slot::Worker), 1);
    }

    #[test]
    fn a_threadless_seat_takes_node_and_group_slots_only() {
        let limits = limits(Some(1));
        assert!(limits.try_start(None, Seat::Take));
        assert!(limits.try_start(N0, Seat::Threadless));
        assert!(!limits.try_start(N0, Seat::Threadless));
        assert!(limits.try_start(N1, Seat::Threadless));
        assert!(!limits.try_start(N2, Seat::Threadless));
        assert_eq!(limits.held(Slot::Worker), 1);
        limits.release(N1, Seat::Threadless);
        assert_eq!(limits.held(Slot::Group(0)), 0);
    }

    #[test]
    fn a_release_never_wraps_and_an_unknown_slot_holds_nothing() {
        let limits = limits(Some(1));
        limits.release(N0, Seat::Take);
        assert_eq!(limits.held(Slot::Worker), 0);
        assert_eq!(limits.held(Slot::Node(NodeId(0))), 0);
        assert_eq!(limits.held(Slot::Node(NodeId(9))), 0);
        assert_eq!(limits.held(Slot::Group(7)), 0);
        // A node past the tables is unlimited.
        assert!(limits.try_start(Some(NodeId(9)), Seat::Threadless));
    }
}
