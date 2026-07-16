"""
Longshot Calibrator
Learns actual resolution rate of low-probability longshot contracts from historical position records.
Adjusts expected value models dynamically based on empirical correction factors.
"""
import logging
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict

_log = logging.getLogger(__name__)

# Implied probability buckets: <2%, 2-4%, 4-6%, 6-8%, 8-10%
_BUCKETS = [
    ("0_2", 0.00, 0.02),
    ("2_4", 0.02, 0.04),
    ("4_6", 0.04, 0.06),
    ("6_8", 0.06, 0.08),
    ("8_10", 0.08, 0.10),
]

class LongshotCalibrator:
    def __init__(self, default_correction: float = 0.60):
        self._default = default_correction
        self._bucket_corrections: Dict[str, float] = {}
        self._sample_counts: Dict[str, int] = {}
        self._last_calibrated: datetime | None = None
        self._lock = RLock()

    async def calibrate(self) -> dict:
        """Query SQLite database for historical positions to calculate empirical correction factors."""
        try:
            from db.database import get_sqlite, _sqlite_lock
            conn = get_sqlite()
            with _sqlite_lock:
                rows = conn.execute("""
                    SELECT entry_price, status
                    FROM poly_yield_positions
                    WHERE strategy = 's6_longshot'
                    AND status IN ('won', 'lost', 'resolved_yes', 'resolved_no')
                """).fetchall()
        except Exception as e:
            _log.warning("[LongshotCalibrator] Database query failed: %s", e)
            return {"error": str(e), "buckets": {}}

        if not rows:
            return {"message": "No historical data to calibrate", "buckets": {}}

        # Group data into buckets
        bucket_data: Dict[str, list] = {b[0]: [] for b in _BUCKETS}
        for row in rows:
            try:
                # S6 buys NO, so entry_price is the NO price. YES price (implied prob) is 1.0 - NO price.
                implied_yes_p = 1.0 - float(row["entry_price"])
            except (ValueError, TypeError):
                continue
            # If we bought NO and the status is 'lost', it means YES won.
            resolved_yes = row["status"] in ("lost", "resolved_yes")
            
            for b_name, lo, hi in _BUCKETS:
                if lo <= implied_yes_p < hi:
                    bucket_data[b_name].append((implied_yes_p, resolved_yes))
                    break

        new_corrections = {}
        new_counts = {}

        for b_name, lo, hi in _BUCKETS:
            data = bucket_data[b_name]
            if len(data) < 5:  # Minimum sample size to calibrate a bucket
                continue
            
            avg_implied = sum(d[0] for d in data) / len(data)
            actual_rate = sum(1 for d in data if d[1]) / len(data)
            
            if avg_implied > 0:
                correction = actual_rate / avg_implied
                new_corrections[b_name] = round(correction, 4)
                new_counts[b_name] = len(data)

        with self._lock:
            self._bucket_corrections = new_corrections
            self._sample_counts = new_counts
            self._last_calibrated = datetime.now(timezone.utc)

        return {
            "buckets": new_corrections,
            "sample_counts": new_counts,
            "calibrated_at": self._last_calibrated.isoformat(),
            "total_positions": len(rows),
        }

    def get_correction(self, implied_prob: float) -> float:
        """Returns the empirical correction factor for a given implied probability, falling back to default."""
        with self._lock:
            if not self._bucket_corrections:
                return self._default
            
            for b_name, lo, hi in _BUCKETS:
                if lo <= implied_prob < hi:
                    return self._bucket_corrections.get(b_name, self._default)
            return self._default

# Global singleton
longshot_calibrator = LongshotCalibrator()
