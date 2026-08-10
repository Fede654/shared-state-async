"""Executable model of shared-state merge/bleach semantics.

This is the *executable counterpart* of doc/protocol-spec-DRAFT.md §6/§8.
Three strategies are modeled:

  v1  — deployed behavior (spec §6.2), including the equal-TTL overwrite
        defect (audit C1) and TTL-as-freshness (audit C6).
  v1i — the *intended* behavior of commit db58e3d (minUpdateTtl actually
        wired in), modeled to quantify how much it would have helped.
  v2  — javierbrk's version-counter merge (spec §8, merge_with_version
        branch commit 522f59d5) including reboot recovery.
  v2r — v2 with a refinement DISCOVERED BY THIS SUITE: the reboot
        recovery leapfrog only applies to entries the node has locally
        (re-)inserted since boot. Without it (plain v2), a rebooted node
        that first hears an OUTDATED echo of its own key and then a
        newer one "recovers" the outdated payload to the highest
        version, resurrecting stale data mesh-wide until its next
        publish (see properties.prop_echo_resurrection).

Pure logic, no I/O: the simulator drives it.
"""

from dataclasses import dataclass, field
from typing import Any, Optional
import copy


@dataclass
class Entry:
    author: str
    ttl: int
    data: Any
    version: int = 0  # used by v2/v2r; 0 == "entry from a non-versioned node"
    local: bool = False  # True iff produced by a local insert since boot (v2r)

    def clone(self) -> "Entry":
        return Entry(self.author, self.ttl, copy.deepcopy(self.data),
                     self.version, self.local)


@dataclass
class MergeResult:
    significant: int = 0
    all_changes: int = 0
    # events: list of (kind, key) with kind in
    #   insert | replace | replace_tiebreak | keep | discard_ill | recover
    events: list = field(default_factory=list)


STRATEGIES = ("v1", "v1i", "v2", "v2r")


def _adopt(local: dict, k: str, e: Entry) -> None:
    ne = e.clone()
    ne.local = False  # entries adopted from the wire are never local-origin
    local[k] = ne


def merge(local: dict, slice_: dict, *, remote: bool, hostname: str,
          strategy: str) -> MergeResult:
    """Merge slice_ into local (mutated in place). Mirrors SharedState::merge."""
    if strategy not in STRATEGIES:
        raise ValueError(strategy)
    r = MergeResult()
    for k, e in slice_.items():
        if k not in local:
            _adopt(local, k, e)
            r.significant += 1
            r.all_changes += 1
            r.events.append(("insert", k))
            continue
        known = local[k]
        own = known.author == hostname

        if strategy in ("v2", "v2r"):
            recovery_applies = remote and own and e.version > known.version
            if strategy == "v2r":
                recovery_applies = recovery_applies and known.local
            if recovery_applies:
                # reboot recovery: keep our data, leapfrog the echo
                known.version = e.version + 1
                r.all_changes += 1
                r.events.append(("recover", k))
                continue
            if e.version > known.version:
                significant = known.data != e.data
                _adopt(local, k, e)
                r.all_changes += 1
                if significant:
                    r.significant += 1
                r.events.append(("replace", k))
            elif e.version == known.version and e.ttl > known.ttl:
                # TTL demoted to tie-break; branch counts no significance
                _adopt(local, k, e)
                r.all_changes += 1
                r.events.append(("replace_tiebreak", k))
            else:
                r.events.append(("keep", k))
            continue

        # v1 family: TTL is the freshness signal
        if remote and own and e.ttl > known.ttl:
            r.events.append(("discard_ill", k))
            continue

        if strategy == "v1i":
            # intended db58e3d guard: own entries need strictly higher TTL
            threshold_ok = e.ttl >= (known.ttl + 1 if (remote and own) else known.ttl)
        else:  # v1 actual: minUpdateTtl computed but never used
            threshold_ok = e.ttl >= known.ttl

        if threshold_ok:
            significant = known.data != e.data
            _adopt(local, k, e)
            r.all_changes += 1
            if significant:
                r.significant += 1
            r.events.append(("replace", k))
        else:
            r.events.append(("keep", k))
    return r


def bleach(local: dict, elapsed: int) -> list:
    """Expire and decrement. Returns list of expired keys.
    Mirrors SharedState::bleach: erase ttl <= elapsed, decrement the rest."""
    if elapsed < 1:
        raise ValueError("bleach called with elapsed < 1")
    expired = [k for k, e in local.items() if e.ttl <= elapsed]
    for k in expired:
        del local[k]
    for e in local.values():
        e.ttl -= elapsed
    return expired


def insert_ttl(strategy: str, bleach_ttl: int, update_interval: int) -> int:
    """TTL assigned by the CLI insert path."""
    if strategy in ("v2", "v2r"):
        return bleach_ttl  # merge_with_version sets plain bleachTTL
    return bleach_ttl + update_interval + 1  # v1 formula


def author_insert(local: dict, *, hostname: str, key: str, data: Any,
                  strategy: str, bleach_ttl: int, update_interval: int) -> None:
    """Model of the CLI insert / periodic publisher path (loopback, own author)."""
    prev = local.get(key)
    version = 0
    if strategy in ("v2", "v2r"):
        version = (prev.version + 1) if prev is not None else 1
    local[key] = Entry(hostname, insert_ttl(strategy, bleach_ttl, update_interval),
                       copy.deepcopy(data), version, local=True)
