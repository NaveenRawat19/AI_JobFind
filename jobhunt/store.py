"""seen.json doubles as the dedupe index AND the application tracker."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .fetch import Job


class Store:
    def __init__(self, path: str | Path = "seen.json"):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                print(f"  ! {self.path} corrupt, starting fresh")

    def unseen(self, jobs: list[Job]) -> list[Job]:
        return [j for j in jobs if j.job_id not in self.data]

    def record(self, jobs: list[Job], emailed: bool) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for j in jobs:
            self.data.setdefault(j.job_id, {
                "first_seen": now,
                "company": j.company,
                "title": j.title,
                "location": j.location,
                "url": j.url,
                "score": j.score,
                "reason": j.reason,
                "emailed": emailed,
                "applied": False,
                "applied_on": None,
            })
        self.save()

    def mark_applied(self, job_id: str) -> bool:
        if job_id not in self.data:
            return False
        self.data[job_id]["applied"] = True
        self.data[job_id]["applied_on"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save()
        return True

    def stats(self) -> dict:
        return {
            "tracked": len(self.data),
            "emailed": sum(1 for v in self.data.values() if v.get("emailed")),
            "applied": sum(1 for v in self.data.values() if v.get("applied")),
        }

    def export_csv(self, path: str | Path = "out/tracker.csv") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = ["first_seen", "company", "title", "location", "score",
                "reason", "applied", "applied_on", "url"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["job_id"] + cols, extrasaction="ignore")
            w.writeheader()
            for jid, row in sorted(self.data.items(),
                                   key=lambda kv: kv[1].get("first_seen", ""), reverse=True):
                w.writerow({"job_id": jid, **row})
        return path

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
