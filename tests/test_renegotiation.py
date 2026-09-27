import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
RENEGOTIATE_DATA = {'monthly_income': 12000.0, 'monthly_expenses': 7000.0, 'proposed_payment': 3000.0, 'reason': '借款人收入下降'}


class RenegotiationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-RE-1", CREATE_DATA)
        record = self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {'assessment_note': '收入波动'})
        record = self.service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})
        self.record = self.service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {'borrower_ack': True})

    def tearDown(self):
        self.temp.cleanup()

    def renegotiate(self, record=None, data=None):
        record = record or self.record
        return self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "renegotiate", data or RENEGOTIATE_DATA)

    def test_renegotiation_flow_and_audit(self):
        original_payment = self.record["payload"]["approved_payment"]
        original_months = self.record["payload"]["approved_months"]
        record = self.renegotiate()
        self.assertEqual(record["state"], "active")
        pending = record["payload"]["pending_renegotiation"]
        self.assertEqual(pending["affordable_payment"], 2500.0)
        self.assertEqual(pending["months"], 5)
        self.assertEqual(pending["reason"], '借款人收入下降')
        # 确认前仍按原值执行
        self.assertEqual(record["payload"]["approved_payment"], original_payment)
        self.assertEqual(record["payload"]["approved_months"], original_months)
        record = self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "confirm_renegotiation", {})
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["payload"]["approved_payment"], 2500.0)
        self.assertEqual(record["payload"]["approved_months"], 5)
        self.assertNotIn("pending_renegotiation", record["payload"])
        timeline = self.service.timeline(Actor("uw", "underwriter"), record["id"])
        confirm_event = timeline[-1]
        self.assertEqual(confirm_event["action"], "confirm_renegotiation")
        self.assertEqual(confirm_event["details"]["previous_plan"], {"approved_payment": original_payment, "approved_months": original_months})
        self.assertEqual(confirm_event["details"]["new_plan"], {"approved_payment": 2500.0, "approved_months": 5})
        self.assertEqual(confirm_event["details"]["reason"], '借款人收入下降')
        submit_event = timeline[-2]
        self.assertEqual(submit_event["action"], "renegotiate")
        self.assertEqual(submit_event["details"]["current_plan"], {"approved_payment": original_payment, "approved_months": original_months})
        self.assertEqual(submit_event["details"]["renegotiation"]["reason"], '借款人收入下降')

    def test_duplicate_renegotiation_is_blocked(self):
        record = self.renegotiate()
        with self.assertRaises(Conflict):
            self.renegotiate(record=record)

    def test_confirm_without_pending_is_blocked(self):
        with self.assertRaises(Conflict):
            self.service.act(Actor("uw", "underwriter"), self.record["id"], self.record["version"], "confirm_renegotiation", {})

    def test_renegotiate_requires_active_state(self):
        other = dict(CREATE_DATA)
        other["borrower_id"] = "borrower-2"
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-RE-2", other)
        with self.assertRaises(Conflict):
            self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "renegotiate", RENEGOTIATE_DATA)

    def test_renegotiation_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("op", "intake_officer"), self.record["id"], self.record["version"], "renegotiate", RENEGOTIATE_DATA)
        record = self.renegotiate()
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "confirm_renegotiation", {})

    def test_renegotiation_validation(self):
        bad = dict(RENEGOTIATE_DATA)
        bad["monthly_expenses"] = 12000.0
        with self.assertRaises(ValidationError):
            self.renegotiate(data=bad)
        bad = dict(RENEGOTIATE_DATA)
        bad["reason"] = ""
        with self.assertRaises(ValidationError):
            self.renegotiate(data=bad)

    def test_second_renegotiation_after_confirm(self):
        record = self.renegotiate()
        record = self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "confirm_renegotiation", {})
        followup = dict(RENEGOTIATE_DATA)
        followup["reason"] = '收入再次变化'
        record = self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "renegotiate", followup)
        self.assertIn("pending_renegotiation", record["payload"])
