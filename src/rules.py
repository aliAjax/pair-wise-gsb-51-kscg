"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
import math
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}, 'renegotiate': {'servicer'}, 'confirm_renegotiation': {'underwriter'}}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}, 'renegotiate': {'active': 'active'}, 'confirm_renegotiation': {'active': 'active'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def prepare_renegotiation(self, record_payload: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """按新的收入、支出和拟还金额重算可承受月供与执行月数。"""
        income = number(data, "monthly_income", 1)
        expenses = number(data, "monthly_expenses", 0)
        offered = number(data, "proposed_payment", 0)
        reason = text(data, "reason")
        if expenses >= income:
            raise ValidationError("支出不能达到或超过收入")
        disposable = income - expenses
        program = record_payload.get("approved_program") or record_payload.get("program_type")
        if program == "deferral":
            affordable = 0.0
        elif program == "reduction":
            affordable = max(0.0, offered - disposable * 0.4)
        else:
            affordable = max(offered * 0.7, disposable * 0.25)
        arrears = float(record_payload.get("arrears", 0.0))
        if affordable <= 0:
            months = 24
        else:
            months = min(24, max(1, math.ceil(arrears / affordable)))
        return {
            "monthly_income": income,
            "monthly_expenses": expenses,
            "proposed_payment": offered,
            "disposable_income": round(disposable, 2),
            "affordable_payment": round(affordable, 2),
            "months": months,
            "reason": reason,
        }

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Dict[str, Any]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        audit_extra: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            summary = "纾困方案生效"
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        elif action == "renegotiate":
            if p.get("pending_renegotiation"):
                raise Conflict("该贷款已存在待确认的重议申请")
            pending = self.prepare_renegotiation(p, data)
            changes["pending_renegotiation"] = pending
            audit_extra["renegotiation"] = {"reason": pending["reason"], "computed": {"affordable_payment": pending["affordable_payment"], "months": pending["months"]}}
            summary = "重议申请已发起，确认前仍按原方案执行"
        elif action == "confirm_renegotiation":
            pending = p.get("pending_renegotiation")
            if not pending:
                raise Conflict("该贷款没有待确认的重议申请")
            previous = {"approved_payment": float(p["approved_payment"]), "approved_months": int(p["approved_months"])}
            changes["monthly_income"] = pending["monthly_income"]
            changes["monthly_expenses"] = pending["monthly_expenses"]
            changes["disposable_income"] = pending["disposable_income"]
            changes["approved_payment"] = pending["affordable_payment"]
            changes["approved_months"] = pending["months"]
            p.pop("pending_renegotiation", None)
            audit_extra["renegotiation"] = {"previous": previous, "current": {"approved_payment": pending["affordable_payment"], "approved_months": pending["months"]}, "reason": pending["reason"]}
            summary = "重议确认：月供%s→%s，执行月数%s→%s" % (previous["approved_payment"], pending["affordable_payment"], previous["approved_months"], pending["months"])
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), audit_extra
