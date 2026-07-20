"""Provider-local admission for the pre-normative dispatch-v2 profile.

This module is deliberately not mounted by ``IicpNode``.  A caller must first
verify the dispatch-ticket trust profile, then explicitly invoke this adapter.
The store records content-free redemption state only and never contacts a
Directory, relay, or inference backend.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

PROFILE = "urn:iicp:profile:dispatch-admission:v2"
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "expired", "rejected"})
_JTI = re.compile(r"^[A-Za-z0-9._:-]{16,256}$")


@dataclass(frozen=True)
class DispatchAdmissionClaim:
    jti: str
    provider_id: str
    intent: str
    not_before: int
    expires_at: int


@dataclass(frozen=True)
class DispatchAdmissionDecision:
    code: str
    accepted: bool = False
    state: str | None = None


@dataclass(frozen=True)
class DispatchAdmissionRecord:
    jti: str
    provider_digest: str
    intent_digest: str
    state: str
    expires_at: int
    consumed_at: int
    updated_at: int


class DispatchAdmissionStorageError(RuntimeError):
    """The provider cannot prove durable single-use admission."""


@runtime_checkable
class DispatchAdmissionStore(Protocol):
    def consume(
        self,
        claim: DispatchAdmissionClaim,
        *,
        expected_provider_id: str,
        expected_intent: str,
        now: int,
        clock_skew_s: int = 0,
    ) -> DispatchAdmissionDecision: ...

    def transition(self, jti: str, state: str, *, now: int) -> DispatchAdmissionRecord: ...
    def cleanup(self, *, now: int, retention_s: int, limit: int) -> int: ...
    def lookup(self, jti: str) -> DispatchAdmissionRecord | None: ...


def evaluate_dispatch_admission(
    store: DispatchAdmissionStore,
    claim: DispatchAdmissionClaim,
    *,
    expected_provider_id: str,
    expected_intent: str,
    now: int,
    trust_verified: bool,
    clock_skew_s: int = 0,
) -> DispatchAdmissionDecision:
    """Compose prior trust verification with provider-local redemption.

    Storage failures intentionally become a content-free, fail-closed result.
    This function does not verify a signature itself; that remains the existing
    dispatch-ticket trust profile's responsibility.
    """

    if not trust_verified:
        return DispatchAdmissionDecision("reject_issuer_key")
    try:
        return store.consume(
            claim,
            expected_provider_id=expected_provider_id,
            expected_intent=expected_intent,
            now=now,
            clock_skew_s=clock_skew_s,
        )
    except DispatchAdmissionStorageError:
        return DispatchAdmissionDecision("reject_store_unavailable")


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _record(row: sqlite3.Row) -> DispatchAdmissionRecord:
    return DispatchAdmissionRecord(
        jti=row["jti"],
        provider_digest=row["provider_digest"],
        intent_digest=row["intent_digest"],
        state=row["state"],
        expires_at=int(row["expires_at"]),
        consumed_at=int(row["consumed_at"]),
        updated_at=int(row["updated_at"]),
    )


class SqliteDispatchAdmissionStore:
    """Single-host durable redemption store for an explicitly opted-in node.

    SQLite is an implementation choice, not protocol state.  The adapter uses
    an immediate transaction and FULL synchronous writes so acceptance is not
    acknowledged before the JTI is durable.  It is not distributed consensus
    and does not protect against rollback of the complete database file.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, *, busy_timeout_s: float = 5.0) -> None:
        self.path = Path(path)
        self.busy_timeout_s = max(0.0, busy_timeout_s)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise DispatchAdmissionStorageError(str(exc)) from exc
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(self.path, timeout=self.busy_timeout_s, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_s * 1000)}")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.Error as exc:
            raise DispatchAdmissionStorageError(str(exc)) from exc

    def _initialize(self) -> None:
        try:
            with self._connect() as db:
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, self.SCHEMA_VERSION}:
                    raise DispatchAdmissionStorageError(f"unsupported dispatch admission database version {version}")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS dispatch_admissions (
                        jti TEXT PRIMARY KEY,
                        provider_digest TEXT NOT NULL,
                        intent_digest TEXT NOT NULL,
                        state TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        consumed_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS dispatch_admissions_expiry
                        ON dispatch_admissions(expires_at);
                    PRAGMA user_version=1;
                    """
                )
            if os.name == "posix":
                os.chmod(self.path, 0o600)
        except DispatchAdmissionStorageError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise DispatchAdmissionStorageError(str(exc)) from exc

    def consume(
        self,
        claim: DispatchAdmissionClaim,
        *,
        expected_provider_id: str,
        expected_intent: str,
        now: int,
        clock_skew_s: int = 0,
    ) -> DispatchAdmissionDecision:
        skew = max(0, int(clock_skew_s))
        if not _JTI.fullmatch(claim.jti):
            return DispatchAdmissionDecision("reject_invalid_jti")
        if claim.provider_id != expected_provider_id:
            return DispatchAdmissionDecision("reject_provider_binding")
        if claim.intent != expected_intent:
            return DispatchAdmissionDecision("reject_intent_binding")
        if now + skew < claim.not_before:
            return DispatchAdmissionDecision("reject_not_yet_valid")
        if now - skew >= claim.expires_at:
            return DispatchAdmissionDecision("reject_expired")

        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM dispatch_admissions WHERE jti=?", (claim.jti,)).fetchone()
            if existing is not None:
                record = _record(existing)
                db.commit()
                code = "reject_terminal" if record.state in TERMINAL_STATES else "reject_replay"
                return DispatchAdmissionDecision(code, state=record.state)
            db.execute(
                "INSERT INTO dispatch_admissions VALUES(?,?,?,?,?,?,?)",
                (
                    claim.jti,
                    _digest(claim.provider_id),
                    _digest(claim.intent),
                    "accepted",
                    claim.expires_at,
                    now,
                    now,
                ),
            )
            db.commit()
            return DispatchAdmissionDecision("accepted", accepted=True, state="accepted")
        except sqlite3.Error as exc:
            db.rollback()
            raise DispatchAdmissionStorageError(str(exc)) from exc
        finally:
            db.close()

    def transition(self, jti: str, state: str, *, now: int) -> DispatchAdmissionRecord:
        if state not in TERMINAL_STATES:
            raise ValueError(f"unsupported terminal admission state: {state}")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM dispatch_admissions WHERE jti=?", (jti,)).fetchone()
            if row is None:
                raise KeyError(jti)
            existing = _record(row)
            if existing.state in TERMINAL_STATES and existing.state != state:
                raise ValueError(f"admission already terminal as {existing.state}")
            if existing.state != state:
                db.execute(
                    "UPDATE dispatch_admissions SET state=?, updated_at=? WHERE jti=?",
                    (state, now, jti),
                )
            row = db.execute("SELECT * FROM dispatch_admissions WHERE jti=?", (jti,)).fetchone()
            db.commit()
            return _record(row)
        except (KeyError, ValueError):
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise DispatchAdmissionStorageError(str(exc)) from exc
        finally:
            db.close()

    def cleanup(self, *, now: int, retention_s: int, limit: int) -> int:
        limit = max(1, int(limit))
        cutoff = now - max(0, int(retention_s))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """DELETE FROM dispatch_admissions WHERE jti IN (
                       SELECT jti FROM dispatch_admissions
                       WHERE expires_at < ? ORDER BY expires_at LIMIT ?
                   )""",
                (cutoff, limit),
            )
            db.commit()
            return max(0, cursor.rowcount)
        except sqlite3.Error as exc:
            db.rollback()
            raise DispatchAdmissionStorageError(str(exc)) from exc
        finally:
            db.close()

    def lookup(self, jti: str) -> DispatchAdmissionRecord | None:
        try:
            with self._connect() as db:
                row = db.execute("SELECT * FROM dispatch_admissions WHERE jti=?", (jti,)).fetchone()
                return _record(row) if row is not None else None
        except sqlite3.Error as exc:
            raise DispatchAdmissionStorageError(str(exc)) from exc
