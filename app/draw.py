"""
Draw engine: pure logic, no web or database code.

State is a plain dict so it can be stored as one JSON document:
    {
      "schedule": {"total": 1000, "survivors": [...], "labels": [...]},
      "owners":   {"17": "Jane Doe", ...},
      "rounds":   [ {round, label, timestamp, seed, started_with, survivors, eliminated}, ... ],
      "undone":   [ same as rounds + "undone_at" ]    # audit trail of undone rounds
    }
"""

from __future__ import annotations

import csv
import io
import random
import re
import secrets
from datetime import datetime, timezone

from . import config


class DrawError(ValueError):
    pass


# Leading bytes of the files people drop into the CSV box by mistake: xlsx/zip,
# legacy .xls compound documents, and PDFs.
BINARY_HEADS = ("PK\x03\x04", "\xd0\xcf\x11\xe0", "%PDF")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f�]")


def looks_binary(text: str) -> bool:
    """True when `text` is a spreadsheet's raw bytes rather than CSV rows."""
    head = text[:4096]
    return head.startswith(BINARY_HEADS) or "\x00" in head or head.count("�") > 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_schedule() -> dict:
    return {
        "total": int(config.TOTAL_TICKETS),
        "survivors": [int(n) for n in config.ROUND_SURVIVORS],
        "labels": [str(s) for s in config.ROUND_LABELS],
        "kinds": [str(s) for s in config.ROUND_KINDS],
        "prizes": [str(s) for s in config.ROUND_PRIZES],
        "completion_label": str(config.COMPLETION_LABEL),
        "completion_label_plural": str(config.COMPLETION_LABEL_PLURAL),
    }


def normalize_schedule(schedule: dict) -> dict:
    survivors = [int(n) for n in schedule["survivors"]]
    labels = [str(s) for s in schedule["labels"]]
    return {
        "total": int(schedule["total"]),
        "survivors": survivors,
        "labels": labels,
        "kinds": [
            str(s)
            for s in schedule.get("kinds", ["elimination"] * len(labels))
        ],
        "prizes": [str(s) for s in schedule.get("prizes", [""] * len(labels))],
        "completion_label": str(schedule.get("completion_label", "Winner")),
        "completion_label_plural": str(
            schedule.get("completion_label_plural", "Winners")
        ),
    }


def validate_schedule(s: dict) -> None:
    lengths = {
        len(s["survivors"]),
        len(s["labels"]),
        len(s["kinds"]),
        len(s["prizes"]),
    }
    if len(lengths) != 1:
        raise DrawError(
            "ROUND_SURVIVORS, ROUND_LABELS, ROUND_KINDS, and ROUND_PRIZES "
            "must be the same length."
        )
    prev = s["total"]
    for index, n in enumerate(s["survivors"]):
        if not 0 < n < prev:
            raise DrawError(
                f"ROUND_SURVIVORS must be strictly decreasing from {s['total']}; got {s['survivors']}."
            )
        kind = s["kinds"][index]
        if kind not in {"elimination", "prize"}:
            raise DrawError(
                "ROUND_KINDS entries must be elimination or prize."
            )
        if kind == "prize" and prev - n != 1:
            raise DrawError(
                "Every prize stage must select exactly one ticket."
            )
        prev = n


def compatible_schedule(
    rounds: list[dict], stored: dict, current: dict
) -> bool:
    """Return whether config can safely replace the remaining stored rounds."""
    if current["total"] != stored["total"]:
        return False
    if len(current["survivors"]) < len(rounds):
        return False
    return all(
        int(rec["survivors"]) == current["survivors"][index]
        for index, rec in enumerate(rounds)
    )


class ReverseDraw:
    def __init__(self, data: dict | None = None) -> None:
        data = data or {}
        rounds = data.get("rounds") or []
        configured_schedule = current_schedule()
        stored_schedule = normalize_schedule(
            data.get("schedule") or configured_schedule
        )
        self.schedule_pending = False
        if rounds and not compatible_schedule(
            rounds, stored_schedule, configured_schedule
        ):
            # Completed targets cannot be rewritten safely. Keep the audited
            # schedule until reset, but expose the mismatch to the admin.
            self.schedule = stored_schedule
            self.schedule_pending = stored_schedule != configured_schedule
        else:
            # Completed targets still match, so config may freely change any
            # future targets or append/remove future rounds. Preserve completed
            # labels because they are part of the historical audit record.
            labels = list(configured_schedule["labels"])
            kinds = list(configured_schedule["kinds"])
            prizes = list(configured_schedule["prizes"])
            for index, rec in enumerate(rounds):
                labels[index] = str(rec["label"])
                kinds[index] = str(
                    rec.get("kind") or stored_schedule["kinds"][index]
                )
                prizes[index] = str(
                    rec.get("prize") or stored_schedule["prizes"][index]
                )
            self.schedule = {
                **configured_schedule,
                "labels": labels,
                "kinds": kinds,
                "prizes": prizes,
            }
        validate_schedule(self.schedule)
        self.owners: dict[int, str] = {}
        self.rounds: list[dict] = []
        self.undone: list[dict] = list(data.get("undone") or [])
        # Serialized pandas split-orient JSON for the original admin upload.
        # It is kept separately from ticket owners so later workflows can use
        # fields such as email without changing draw allocation behavior.
        self.source_dataframe: dict | None = data.get("source_dataframe")
        self.allocation_source_fingerprint: str = str(
            data.get("allocation_source_fingerprint") or ""
        )
        self.notification_batches: list[dict] = list(
            data.get("notification_batches") or []
        )
        self.eliminated_in: dict[int, int] = {}
        self.set_owners({int(k): v for k, v in (data.get("owners") or {}).items()})
        for rec in rounds:
            self._apply(rec)

    # ---- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schedule": self.schedule,
            "owners": {str(k): v for k, v in sorted(self.owners.items())},
            "rounds": self.rounds,
            "undone": self.undone,
            "source_dataframe": self.source_dataframe,
            "allocation_source_fingerprint": self.allocation_source_fingerprint,
            "notification_batches": self.notification_batches,
        }

    # ---- queries ------------------------------------------------------------
    @property
    def total(self) -> int:
        return self.schedule["total"]

    @property
    def labels(self) -> list[str]:
        return self.schedule["labels"]

    @property
    def survivors(self) -> list[int]:
        return self.schedule["survivors"]

    @property
    def kinds(self) -> list[str]:
        return self.schedule["kinds"]

    @property
    def prizes(self) -> list[str]:
        return self.schedule["prizes"]

    @property
    def rounds_done(self) -> int:
        return len(self.rounds)

    @property
    def started(self) -> bool:
        return bool(self.rounds)

    @property
    def finished(self) -> bool:
        return self.rounds_done >= len(self.survivors)

    def tickets(self) -> range:
        return range(1, self.total + 1)

    def active(self) -> list[int]:
        return [t for t in self.tickets() if t not in self.eliminated_in]

    def winners(self) -> list[int]:
        return self.active() if self.finished else []

    def holder(self, t: int) -> str:
        return self.owners.get(t, "")

    def status(self, t: int) -> str:
        r = self.eliminated_in.get(t)
        if r:
            rec = self.rounds[r - 1]
            if rec.get("kind") == "prize":
                return f"{rec.get('prize') or rec['label']} winner"
            return f"Eliminated in {self.labels[r - 1]}"
        if self.finished:
            return self.schedule["completion_label"].upper()
        return "Still in"

    def status_array(self) -> list[int]:
        """Index i -> round ticket i+1 was eliminated in (0 = still in)."""
        return [self.eliminated_in.get(t, 0) for t in self.tickets()]

    # ---- actions ------------------------------------------------------------
    def _apply(self, rec: dict) -> None:
        rec = dict(rec)
        index = int(rec["round"]) - 1
        rec.setdefault("kind", self.kinds[index])
        rec.setdefault("prize", self.prizes[index])
        self.rounds.append(rec)
        for t in rec["eliminated"]:
            self.eliminated_in[t] = rec["round"]

    def run_next_round(self) -> dict:
        if self.finished:
            raise DrawError("The draw is already complete.")
        idx = self.rounds_done
        pool = self.active()
        target = self.survivors[idx]
        seed = (config.RANDOM_SEED + idx) if config.RANDOM_SEED is not None else secrets.randbits(64)
        keep = set(random.Random(seed).sample(pool, target))
        rec = {
            "round": idx + 1,
            "label": self.labels[idx],
            "kind": self.kinds[idx],
            "prize": self.prizes[idx],
            "timestamp": now_iso(),
            "seed": str(seed),
            "started_with": len(pool),
            "survivors": target,
            "eliminated": sorted(t for t in pool if t not in keep),
        }
        self._apply(rec)
        return rec

    def undo_last_round(self) -> dict:
        if not self.rounds:
            raise DrawError("No rounds to undo.")
        rec = self.rounds.pop()
        for t in rec["eliminated"]:
            self.eliminated_in.pop(t, None)
        self.undone.append({**rec, "undone_at": now_iso()})
        return rec

    def reset(self, keep_owners: bool = True) -> None:
        self.rounds.clear()
        self.eliminated_in.clear()
        self.undone.clear()
        self.schedule = current_schedule()
        self.schedule_pending = False
        validate_schedule(self.schedule)
        if not keep_owners:
            self.owners.clear()
            self.source_dataframe = None
            self.allocation_source_fingerprint = ""
            self.notification_batches.clear()

    def set_owners(self, mapping: dict[int, str]) -> None:
        clean: dict[int, str] = {}
        for t, n in mapping.items():
            # Strip control characters so stray bytes can never become a holder.
            t, n = int(t), CONTROL_CHARS.sub("", str(n or "")).strip()
            if 1 <= t <= self.total and n:
                clean[t] = n[:120]
        self.owners = clean

    def assign_block(self, name: str, start: int, end: int) -> int:
        name = name.strip()
        if not name:
            raise DrawError("Name is required.")
        lo, hi = sorted((int(start), int(end)))
        lo, hi = max(lo, 1), min(hi, self.total)
        if lo > hi:
            raise DrawError(f"Ticket range must be within 1-{self.total}.")
        self.set_owners({**self.owners, **{t: name for t in range(lo, hi + 1)}})
        return hi - lo + 1

    def unassign_block(self, start: int, end: int) -> int:
        lo, hi = sorted((int(start), int(end)))
        lo, hi = max(lo, 1), min(hi, self.total)
        if lo > hi:
            raise DrawError(f"Ticket range must be within 1-{self.total}.")
        removed = sum(ticket in self.owners for ticket in range(lo, hi + 1))
        self.set_owners(
            {
                ticket: holder
                for ticket, holder in self.owners.items()
                if not lo <= ticket <= hi
            }
        )
        return removed

    def unassign_holder(self, name: str) -> int:
        key = re.sub(r"\s+", " ", name).strip().casefold()
        if not key:
            raise DrawError("Holder name is required.")
        keep = {
            ticket: holder
            for ticket, holder in self.owners.items()
            if re.sub(r"\s+", " ", holder).strip().casefold() != key
        }
        removed = len(self.owners) - len(keep)
        self.set_owners(keep)
        return removed

    # ---- CSV ------------------------------------------------------------------
    def parse_owner_csv(self, text: str) -> dict[int, str]:
        """Rows of `ticket,name`. A header row or junk rows are skipped."""
        if looks_binary(text):
            raise DrawError(
                "That is a spreadsheet file's raw contents, not ticket,name rows. "
                "Use “Load CSV or Excel…” to upload the file instead of "
                "pasting or dropping it into the box."
            )
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error as error:
            raise DrawError(f"Could not read those rows: {error}") from error
        out: dict[int, str] = {}
        for row in rows:
            if len(row) < 2:
                continue
            try:
                t = int(row[0].strip())
            except ValueError:
                continue
            name = ",".join(row[1:]).strip()
            if name and 1 <= t <= self.total:
                out[t] = name
        return out

    def owners_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ticket", "name"])
        for t, n in sorted(self.owners.items()):
            w.writerow([t, n])
        return buf.getvalue()

    def tickets_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Ticket", "Holder", "Status", "Eliminated round"])
        for t in self.tickets():
            r = self.eliminated_in.get(t)
            w.writerow([t, self.holder(t), self.status(t), self.labels[r - 1] if r else ""])
        return buf.getvalue()

    def log_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Round", "Round label", "Timestamp (UTC)", "Seed", "Ticket", "Holder"])
        for rec in self.rounds:
            for t in rec["eliminated"]:
                w.writerow([rec["round"], rec["label"], rec["timestamp"], rec["seed"], t, self.holder(t)])
        return buf.getvalue()

    def holder_summary(self) -> list[dict]:
        people: dict[str, dict] = {}
        for t, name in sorted(self.owners.items()):
            p = people.setdefault(name, {"holder": name, "tickets": [], "still_in": 0})
            p["tickets"].append(t)
            if t not in self.eliminated_in:
                p["still_in"] += 1
        return sorted(people.values(), key=lambda p: (-p["still_in"], p["holder"].lower()))
