"""糖化、麦汁、煮沸与酒花顺序约束。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import InterlockError, SequenceError, ValidationError

from .helpers import StepClock, create_batch, make_app, mash_to_filter


class MashFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing

    def test_charge_requires_persisted_water_confirmation(self) -> None:
        batch_id = create_batch(self.app)
        with self.assertRaises(SequenceError):
            self.brewing.charge_mash(batch_id, 220.0, "tester")
        self.brewing.confirm_water(batch_id, 52.0, "probe-mash", "tester")
        view = self.brewing.charge_mash(batch_id, 220.0, "tester")
        self.assertEqual("charged", view["mash"]["stage"])
        self.assertIsNotNone(view["mash"]["water_confirmed_at"])

    def test_water_out_of_tolerance_is_rejected(self) -> None:
        batch_id = create_batch(self.app)
        with self.assertRaises(InterlockError):
            self.brewing.confirm_water(batch_id, 61.0, "probe-mash", "tester")
        self.assertEqual("awaiting_water", self.brewing.status(batch_id)["mash"]["stage"])

    def test_filter_validates_gravity_against_recipe(self) -> None:
        batch_id = create_batch(self.app)
        self.brewing.confirm_water(batch_id, 52.0, "probe-mash", "tester")
        self.brewing.charge_mash(batch_id, 220.0, "tester")
        self.brewing.heat_mash(batch_id, 65.0, "tester")
        self.brewing.rest_mash(batch_id, 65.0, "tester")
        with self.assertRaises(ValidationError):
            self.brewing.filter_mash(batch_id, 9.9, 1020.0, 5.4, "tester")

    def test_full_boil_chain_and_hop_windows(self) -> None:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        view = self.brewing.status(batch_id)
        self.assertEqual("boiling", view["batch"]["stage"])
        self.brewing.ignite_boil(batch_id, "tester")
        self.brewing.boil_rolling(batch_id, "tester")
        with self.assertRaises(SequenceError):
            self.brewing.add_hop(batch_id, 2, 47.0, "tester")
        self.brewing.add_hop(batch_id, 1, 1.0, "tester")
        with self.assertRaises(InterlockError):
            self.brewing.add_hop(batch_id, 2, 30.0, "tester")
        with self.assertRaises(Exception):
            self.brewing.whirlpool(batch_id, 59.0, "tester")
        self.clock.advance(60)
        self.brewing.whirlpool(batch_id, 59.0, "tester")
        view = self.brewing.status(batch_id)
        self.assertEqual("cooling", view["batch"]["stage"])
        self.assertEqual(1, view["hops"]["added"])
        self.assertEqual(2, view["hops"]["missed"])

    def test_hop_addition_order_is_enforced(self) -> None:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        self.brewing.ignite_boil(batch_id, "tester")
        self.brewing.boil_rolling(batch_id, "tester")
        with self.assertRaises(SequenceError):
            self.brewing.add_hop(batch_id, 3, 55.0, "tester")
