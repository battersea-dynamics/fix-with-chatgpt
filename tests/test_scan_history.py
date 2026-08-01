import json
import tempfile
import unittest
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.scan_history import build_momentum_shadow


ET = ZoneInfo("America/New_York")


@dataclass
class ScanResult:
    symbol: str
    sector: str
    close: float
    rel_volume: float
    pct_change: float
    ma_distance: float
    score: float
    catalyst: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def result(
    symbol: str,
    close: float,
    rel_volume: float,
    score: float,
    pct_change: float = 5.0,
    ma_distance: float = 4.0,
) -> ScanResult:
    return ScanResult(
        symbol=symbol,
        sector="test",
        close=close,
        rel_volume=rel_volume,
        pct_change=pct_change,
        ma_distance=ma_distance,
        score=score,
    )


def write_scan(path: Path, at: datetime, rows: list[ScanResult]) -> None:
    path.write_text(json.dumps({
        "generated_at": at.isoformat(timespec="seconds"),
        "shortlist": [row.to_dict() for row in rows],
    }))


class ScanHistoryTests(unittest.TestCase):
    def test_first_scan_has_insufficient_history(self):
        with tempfile.TemporaryDirectory() as folder:
            current = [result("ABC", 10.0, 0.5, 3.0)]
            shadow = build_momentum_shadow(
                current,
                datetime(2026, 7, 30, 10, 15, tzinfo=ET),
                Path(folder),
            )

        context = shadow["symbols"]["ABC"]
        self.assertFalse(shadow["affects_decisions"])
        self.assertEqual(context["observations"], 1)
        self.assertEqual(context["consecutive_scans"], 1)
        self.assertEqual(context["setup"], "insufficient_history")
        self.assertIsNone(context["return_since_previous_pct"])

    def test_consecutive_rising_scans_build_sustained_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_scan(
                root / "shortlist_1015.json",
                datetime(2026, 7, 30, 10, 15, tzinfo=ET),
                [result("ABC", 10.0, 0.50, 3.0), result("XYZ", 5, 1, 4)],
            )
            write_scan(
                root / "shortlist_1045.json",
                datetime(2026, 7, 30, 10, 45, tzinfo=ET),
                [result("ABC", 10.5, 0.65, 4.0)],
            )

            shadow = build_momentum_shadow(
                [result("ABC", 11.0, 0.85, 5.5)],
                datetime(2026, 7, 30, 11, 15, tzinfo=ET),
                root,
            )

        context = shadow["symbols"]["ABC"]
        self.assertEqual(context["observations"], 3)
        self.assertEqual(context["consecutive_scans"], 3)
        self.assertEqual(context["positive_observed_intervals"], 2)
        self.assertEqual(context["return_since_first_seen_pct"], 10.0)
        self.assertEqual(context["drawdown_from_observed_high_pct"], 0.0)
        self.assertEqual(context["previous_rank"], 1)
        self.assertEqual(context["setup"], "sustained_continuation")

    def test_missing_immediately_previous_scan_breaks_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_scan(
                root / "shortlist_1015.json",
                datetime(2026, 7, 30, 10, 15, tzinfo=ET),
                [result("ABC", 10.0, 0.5, 3.0)],
            )
            write_scan(
                root / "shortlist_1045.json",
                datetime(2026, 7, 30, 10, 45, tzinfo=ET),
                [result("XYZ", 6.0, 0.7, 4.0)],
            )

            shadow = build_momentum_shadow(
                [result("ABC", 10.8, 0.8, 4.5)],
                datetime(2026, 7, 30, 11, 15, tzinfo=ET),
                root,
            )

        context = shadow["symbols"]["ABC"]
        self.assertEqual(context["observations"], 2)
        self.assertEqual(context["consecutive_scans"], 1)
        self.assertIsNone(context["return_since_previous_pct"])
        self.assertEqual(context["setup"], "insufficient_history")

    def test_other_session_and_malformed_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_scan(
                root / "shortlist_1545.json",
                datetime(2026, 7, 29, 15, 45, tzinfo=ET),
                [result("ABC", 8.0, 1.0, 4.0)],
            )
            (root / "shortlist_1045.json").write_text("not json")

            shadow = build_momentum_shadow(
                [result("ABC", 10.0, 0.5, 3.0)],
                datetime(2026, 7, 30, 10, 15, tzinfo=ET),
                root,
            )

        self.assertEqual(shadow["prior_scans_available"], 0)
        self.assertEqual(shadow["symbols"]["ABC"]["observations"], 1)

    def test_falling_knife_rebound_is_descriptive_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_scan(
                root / "shortlist_1015.json",
                datetime(2026, 7, 30, 10, 15, tzinfo=ET),
                [result("ABC", 8.0, 1.0, 5.0, -20.0, -15.0)],
            )

            shadow = build_momentum_shadow(
                [result("ABC", 8.4, 1.2, 5.2, -16.0, -12.0)],
                datetime(2026, 7, 30, 10, 45, tzinfo=ET),
                root,
            )

        self.assertEqual(
            shadow["symbols"]["ABC"]["setup"],
            "falling_knife_rebound",
        )
        self.assertFalse(shadow["affects_decisions"])


if __name__ == "__main__":
    unittest.main()
