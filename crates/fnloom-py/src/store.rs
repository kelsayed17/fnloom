//! The in-memory result store behind `fnloom.MemoryCache`, least recently used evicted first.

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hash, Hasher};
use std::sync::{Mutex, MutexGuard, PoisonError};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use pyo3::{PyTraverseError, PyVisit};

use crate::fingerprint::Digest;

/// The end of the recency list.
const NIL: usize = usize::MAX;

/// A key, hashed by folding its four 8-byte words together. A key the cache makes is a
/// BLAKE3 digest, already uniform, but a caller's may not be, so every byte counts.
#[derive(Clone, Copy, PartialEq, Eq)]
struct Key(Digest);

impl Hash for Key {
    fn hash<H: Hasher>(&self, state: &mut H) {
        let folded = self.0.chunks_exact(8).fold(0_u64, |folded, word| {
            let mut bytes = [0; 8];
            bytes.copy_from_slice(word);
            folded.rotate_left(17) ^ u64::from_le_bytes(bytes)
        });
        // The splitmix64 finalizer, so every input bit reaches the low bits a table
        // indexes by.
        let mut mixed = folded;
        mixed = (mixed ^ (mixed >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        mixed = (mixed ^ (mixed >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        state.write_u64(mixed ^ (mixed >> 31));
    }
}

/// The hasher of `Key`: the one `u64` it is given.
#[derive(Default)]
struct KeyHasher(u64);

impl Hasher for KeyHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for &byte in bytes {
            self.0 = self.0.rotate_left(8) ^ u64::from(byte);
        }
    }

    fn write_u64(&mut self, value: u64) {
        self.0 = value;
    }
}

/// One stored value, linked into the recency list by slot index.
struct Entry {
    key: Key,
    value: Py<PyAny>,
    size: usize,
    /// The next older entry, or `NIL`.
    older: usize,
    /// The next newer entry, or `NIL`.
    newer: usize,
}

/// The entries, by key and in recency order; every method runs under the store's lock.
#[derive(Default)]
struct Lru {
    index: HashMap<Key, usize, BuildHasherDefault<KeyHasher>>,
    /// Entries by slot; a freed slot is `None` and listed in `free`.
    slots: Vec<Option<Entry>>,
    free: Vec<usize>,
    /// The least recently used entry, or `NIL`.
    oldest: usize,
    /// The most recently used entry, or `NIL`.
    newest: usize,
    bytes: usize,
}

impl Lru {
    fn new() -> Self {
        Self {
            oldest: NIL,
            newest: NIL,
            ..Self::default()
        }
    }

    fn entry(&self, slot: usize) -> Option<&Entry> {
        self.slots.get(slot).and_then(Option::as_ref)
    }

    fn entry_mut(&mut self, slot: usize) -> Option<&mut Entry> {
        self.slots.get_mut(slot).and_then(Option::as_mut)
    }

    /// Takes `slot` out of the recency list, leaving its entry in place.
    fn unlink(&mut self, slot: usize) {
        let Some(entry) = self.entry(slot) else {
            return;
        };
        let (older, newer) = (entry.older, entry.newer);
        match self.entry_mut(older) {
            Some(entry) => entry.newer = newer,
            None => self.oldest = newer,
        }
        match self.entry_mut(newer) {
            Some(entry) => entry.older = older,
            None => self.newest = older,
        }
    }

    /// Puts `slot` at the newest end of the recency list.
    fn link_newest(&mut self, slot: usize) {
        let newest = self.newest;
        if let Some(entry) = self.entry_mut(slot) {
            entry.older = newest;
            entry.newer = NIL;
        }
        match self.entry_mut(newest) {
            Some(entry) => entry.newer = slot,
            None => self.oldest = slot,
        }
        self.newest = slot;
    }

    /// The value under `key`, made the most recently used.
    fn get(&mut self, py: Python<'_>, key: &Key) -> Option<Py<PyAny>> {
        let slot = *self.index.get(key)?;
        if self.newest != slot {
            self.unlink(slot);
            self.link_newest(slot);
        }
        self.entry(slot).map(|entry| entry.value.clone_ref(py))
    }

    /// Removes the entry at `slot`, returning it.
    fn remove_slot(&mut self, slot: usize) -> Option<Entry> {
        self.unlink(slot);
        let entry = self.slots.get_mut(slot)?.take()?;
        self.index.remove(&entry.key);
        self.free.push(slot);
        self.bytes -= entry.size;
        Some(entry)
    }

    fn remove(&mut self, key: &Key) -> Option<Entry> {
        let slot = *self.index.get(key)?;
        self.remove_slot(slot)
    }

    /// Stores `value` as the newest entry.
    fn insert(&mut self, key: Key, value: Py<PyAny>, size: usize) {
        let entry = Entry {
            key,
            value,
            size,
            older: NIL,
            newer: NIL,
        };
        let slot = if let Some(slot) = self.free.pop() {
            if let Some(free) = self.slots.get_mut(slot) {
                *free = Some(entry);
            }
            slot
        } else {
            self.slots.push(Some(entry));
            self.slots.len() - 1
        };
        self.index.insert(key, slot);
        self.bytes += size;
        self.link_newest(slot);
    }
}

/// Results held in memory, least recently used evicted first; safe to share between threads.
///
/// `sizeof`, given with `max_bytes`, sizes each value before it is stored: an `int`, or
/// `None` to skip the write (it has warned already). Values are dropped, and their
/// finalizers run, only after the lock is released.
#[pyclass(frozen, module = "fnloom._core")]
pub struct MemoryStore {
    max_entries: usize,
    max_bytes: Option<usize>,
    sizeof: Option<Py<PyAny>>,
    lru: Mutex<Lru>,
}

impl MemoryStore {
    fn lock(&self) -> MutexGuard<'_, Lru> {
        // A panic inside the lock leaves the list consistent at each step; go on.
        self.lru.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The value under `key`, the same object stored, made the most recently used.
    pub fn load(&self, py: Python<'_>, key: &Digest) -> Option<Py<PyAny>> {
        self.lock().get(py, &Key(*key))
    }

    /// Stores `value` under `key`, evicting the least recently used past a bound.
    pub fn store(&self, py: Python<'_>, key: &Digest, value: Py<PyAny>) -> PyResult<()> {
        let size = match (&self.sizeof, self.max_bytes) {
            (Some(sizeof), Some(_)) => {
                let size = sizeof.bind(py).call1((value.bind(py),))?;
                if size.is_none() {
                    return Ok(());
                }
                size.extract::<usize>()?
            }
            _ => 0,
        };
        // Released after the lock, in case releasing one runs code that uses the store.
        let mut evicted: Vec<Entry> = Vec::new();
        {
            let mut lru = self.lock();
            let key = Key(*key);
            evicted.extend(lru.remove(&key));
            // A value over the bound on its own is not stored, and evicts nothing.
            if self.max_bytes.is_some_and(|bound| size > bound) {
                drop(lru);
                drop(evicted);
                return Ok(());
            }
            lru.insert(key, value, size);
            while lru.index.len() > self.max_entries
                || self.max_bytes.is_some_and(|bound| lru.bytes > bound)
            {
                let oldest = lru.oldest;
                match lru.remove_slot(oldest) {
                    Some(entry) => evicted.push(entry),
                    None => break,
                }
            }
        }
        drop(evicted);
        Ok(())
    }
}

/// `key` as a digest; `ValueError` unless it is 32 bytes.
fn digest_of(key: &[u8]) -> PyResult<Digest> {
    key.try_into()
        .map_err(|_| PyValueError::new_err(format!("a cache key is 32 bytes, not {}", key.len())))
}

#[pymethods]
impl MemoryStore {
    /// Makes an empty store with these bounds, already checked by `MemoryCache`.
    #[new]
    #[pyo3(signature = (max_entries, max_bytes = None, sizeof = None))]
    fn new(max_entries: usize, max_bytes: Option<usize>, sizeof: Option<Py<PyAny>>) -> Self {
        Self {
            max_entries,
            max_bytes,
            sizeof,
            lru: Mutex::new(Lru::new()),
        }
    }

    /// The value stored under `key`, or `default`.
    fn get(&self, py: Python<'_>, key: &[u8], default: Py<PyAny>) -> PyResult<Py<PyAny>> {
        Ok(self.load(py, &digest_of(key)?).unwrap_or(default))
    }

    /// Stores `value` under `key`, replacing any value there.
    fn put(&self, py: Python<'_>, key: &[u8], value: Py<PyAny>) -> PyResult<()> {
        self.store(py, &digest_of(key)?, value)
    }

    /// Forgets `key`, if the store holds it.
    fn discard(&self, key: &[u8]) -> PyResult<()> {
        let key = Key(digest_of(key)?);
        let removed = self.lock().remove(&key);
        drop(removed);
        Ok(())
    }

    /// The number of entries.
    fn __len__(&self) -> usize {
        self.lock().index.len()
    }

    /// Every `(key, value)` pair, oldest first.
    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let pairs: Vec<(Digest, Py<PyAny>)> = {
            let lru = self.lock();
            let mut pairs = Vec::with_capacity(lru.index.len());
            let mut slot = lru.oldest;
            while let Some(entry) = lru.entry(slot) {
                pairs.push((entry.key.0, entry.value.clone_ref(py)));
                slot = entry.newer;
            }
            pairs
        };
        PyList::new(
            py,
            pairs
                .into_iter()
                .map(|(key, value)| (PyBytes::new(py, &key), value)),
        )
    }

    /// Lets the cyclic GC follow the stored values.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(sizeof) = &self.sizeof {
            visit.call(sizeof)?;
        }
        // The lock is never held across a call into Python, so the collector, which
        // runs only between such calls, finds it free; if not, report nothing.
        if let Ok(lru) = self.lru.try_lock() {
            for entry in lru.slots.iter().flatten() {
                visit.call(&entry.value)?;
            }
        }
        Ok(())
    }

    /// Drops every value, to break a cycle through the store.
    fn __clear__(&self) {
        let lru = std::mem::replace(&mut *self.lock(), Lru::new());
        drop(lru);
    }
}
