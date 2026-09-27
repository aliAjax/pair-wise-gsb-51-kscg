import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
RENEGOTIATE_DATA = {'monthly_income': 12000.0, 'monthly_expenses': 7000.0, 'proposed_payment': 5000.0, 'reason': '收入再次下降'}


def activate(service, reference="MORT-27001"):
    record = service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
    record = service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
    record = service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
    return service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {"borrower_ack": True})


class RenegotiationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_renegotiate_and_confirm(self):
        record = activate(self.service)
        old_payment = record["payload"]["approved_payment"]
        old_months = record["payload"]["approved_months"]
        record = self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "renegotiate", RENEGOTIATE_DATA)
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["payload"]["approved_payment"], old_payment)
        self.assertEqual(record["payload"]["approved_months"], old_months)
        pending = record["payload"]["pending_renegotiation"]
        self.assertEqual(pending["affordable_payment"], 3000.0)
        self.assertEqual(pending["months"], 4)
        self.assertEqual(pending["reason"], "收入再次下降")
        record = self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "confirm_renegotiation", {})
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["payload"]["approved_payment"], 3000.0)
        self.assertEqual(record["payload"]["approved_months"], 4)
        self.assertEqual(record["payload"]["monthly_income"], 12000.0)
        self.assertNotIn("pending_renegotiation", record["payload"])
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        confirm_events = [event for event in timeline if event["action"] == "confirm_renegotiation"]
        self.assertEqual(len(confirm_events), 1)
        audit = confirm_events[0]["details"]["renegotiation"]
        self.assertEqual(audit["previous"], {"approved_payment": old_payment, "approved_months": old_months})
        self.assertEqual(audit["current"], {"approved_payment": 3000.0, "approved_months": 4})
        self.assertEqual(audit["reason"], "收入再次下降")

    def test_duplicate_renegotiation_blocked(self):
        record = activate(self.service)
        record = self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "renegotiate", RENEGOTIATE_DATA)
        with self.assertRaises(Conflict):
            self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "renegotiate", RENEGOTIATE_DATA)

    def test_confirm_without_pending_blocked(self):
        record = activate(self.service)
        with self.assertRaises(Conflict):
            self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "confirm_renegotiation", {})

    def test_role_and_state_guards(self):
        record = activate(self.service)
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "renegotiate", RENEGOTIATE_DATA)
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("svc", "servicer"), record["id"], record["version"], "confirm_renegotiation", {})
        other = dict(CREATE_DATA, borrower_id="borrower-2")
        fresh = self.service.create(Actor("creator", "intake_officer"), "MORT-27002", other)
        with self.assertRaises(Conflict):
            self.service.act(Actor("svc", "servicer"), fresh["id"], fresh["version"], "renegotiate", RENEGOTIATE_DATA)
