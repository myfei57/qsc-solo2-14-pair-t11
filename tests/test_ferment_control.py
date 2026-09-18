"""发酵、温控、压力联锁与 CIP 清洗流程。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import InterlockError, LatchError, SequenceError

from .helpers import StepClock, boil_to_cooling, create_batch, first_tank, make_app, mash_to_filter, sanitize_tank


class FermentControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing
        self.control = self.app.registry.control
        self.maintenance = self.app.registry.maintenance

    def _batch_ready_to_transfer(self) -> str:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        boil_to_cooling(self.app, batch_id)
        self.brewing.mark_cooled(batch_id, 10.0, "tester")
        return batch_id

    def test_transfer_requires_sanitized_tank(self) -> None:
        batch_id = self._batch_ready_to_transfer()
        with self.assertRaises(SequenceError):
            self.brewing.transfer_to_tank(batch_id, first_tank(self.app), "tester")
        sanitize_tank(self.app, first_tank(self.app))
        view = self.brewing.transfer_to_tank(batch_id, first_tank(self.app), "tester")
        self.assertIsNotNone(view["batch"]["cip_certificate_id"])

    def test_pitch_requires_cooling_reached(self) -> None:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        self.brewing.ignite_boil(batch_id, "tester")
        self.brewing.boil_rolling(batch_id, "tester")
        self.brewing.add_hop(batch_id, 1, 1.0, "tester")
        self.brewing.add_hop(batch_id, 2, 47.0, "tester")
        self.brewing.add_hop(batch_id, 3, 55.0, "tester")
        self.clock.advance(60)
        self.brewing.whirlpool(batch_id, 59.0, "tester")
        tank_id = first_tank(self.app)
        sanitize_tank(self.app, tank_id)
        with self.assertRaises(InterlockError):
            self.brewing.transfer_to_tank(batch_id, tank_id, "tester")

    def test_pressure_latch_blocks_valves_and_requires_reset(self) -> None:
        tank_id = first_tank(self.app)
        state = self.control.set_pressure(tank_id, 3.0, "tester")
        self.assertEqual("latched", state["state"])
        with self.assertRaises(LatchError):
            self.control.operate_valve(tank_id, "transfer", "open", "tester")
        with self.assertRaises(InterlockError):
            self.control.reset_latch(tank_id, "tester")
        self.control.set_pressure(tank_id, 1.0, "tester")
        reset = self.control.reset_latch(tank_id, "tester")
        self.assertEqual("normal", reset["state"])

    def test_cip_sequence_must_be_complete(self) -> None:
        tank_id = first_tank(self.app)
        cycle = self.maintenance.start_clean(tank_id, "tester")
        self.assertEqual("prerinse", cycle["stage"])
        advanced = self.maintenance.advance_clean(str(cycle["id"]), "tester")
        self.assertEqual("caustic", advanced["stage"])
        finished = self.maintenance.finish_clean(str(cycle["id"]), "tester")
        self.assertEqual("complete", finished["cycle"]["stage"])
        certificate = self.maintenance.certificate_status(tank_id)
        self.assertTrue(certificate["valid"])

    def test_certificate_expiry_blocks_transfer(self) -> None:
        tank_id = first_tank(self.app)
        sanitize_tank(self.app, tank_id)
        self.clock.advance(self.app.settings.cip_certificate_ttl_min + 5)
        report = self.maintenance.certificate_status(tank_id)
        self.assertFalse(report["valid"])

    def test_probe_quality_and_baseline_lag(self) -> None:
        telemetry = self.app.registry.telemetry
        probe = str(self.app.registry.temp.probes.all()[0]["id"])
        reading = telemetry.ingest(probe, 82.0, None, "tester")
        self.assertEqual("rejected", reading["quality"])
        for _ in range(6):
            telemetry.ingest(probe, 67.0, None, "tester")
        report = telemetry.probe_report(probe, window=5)
        self.assertTrue(report["lagging"])
