//! How many tasks may run at once: the `Grant` a scheduler asks before it hands one out.

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
