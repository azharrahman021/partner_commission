import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from partner_commission import commission

FIELD_TYPES = {
	"commission_amount": "Currency",
	"potential_commission_amount": "Currency",
	"commission_base": "Currency",
	"commission_rate": "Percent",
	"posting_date": "Date",
	"latest_payment_date": "Date",
	"docstatus": "Int",
}
TEXT_FIELDS = (
	"sales_invoice",
	"sales_partner",
	"sales_person",
	"commission_type",
	"company",
	"status",
	"eligibility_status",
	"ineligibility_reason",
	"sync_key",
	"custom_sync_key",
)


class Ledger:
	"""Database boundary double; business reconciliation runs without a site."""

	def __init__(self):
		self.values = dict.fromkeys([*FIELD_TYPES, *TEXT_FIELDS])
		self.values["docstatus"] = 0
		self.meta = SimpleNamespace(
			has_field=lambda key: key in self.values,
			get_field=lambda key: SimpleNamespace(fieldtype=FIELD_TYPES.get(key, "Data")),
		)

	def set(self, key, value):
		self.values[key] = value

	def get_valid_dict(self, **kwargs):
		return dict(self.values)


class TestCommissionReconciliation(unittest.TestCase):
	def setUp(self):
		self.invoice = SimpleNamespace(name="INV-1", grand_total=1000, net_total=1000)
		self.rows = []
		self.paid = ({}, {})
		self.inserted = []
		self.target = {
			"sales_invoice": "INV-1",
			"sales_partner": "Partner A",
			"sales_person": None,
			"commission_type": "Sales Partner",
			"company": "Company A",
			"commission_amount": 50,
			"potential_commission_amount": 50,
			"commission_base": 1000,
			"commission_rate": 5,
			"status": "Unpaid",
			"eligibility_status": "Eligible",
			"ineligibility_reason": None,
			"posting_date": "2026-09-22",
			"latest_payment_date": None,
		}
		self.start_patch("get_paid_commission_totals", side_effect=lambda name: self.paid)
		self.start_patch("frappe.new_doc", side_effect=lambda doctype: Ledger())
		self.get_all = self.start_patch("frappe.get_all", side_effect=lambda *a, **k: self.rows)
		self.deleted = self.start_patch("delete_unpaid_ledgers_for_invoice")
		self.start_patch(
			"insert_commission_ledger", side_effect=lambda values: self.inserted.append(dict(values))
		)

	def start_patch(self, name, **kwargs):
		patcher = patch("partner_commission.commission." + name, **kwargs)
		self.addCleanup(patcher.stop)
		return patcher.start()

	def stored(self, values=None, amount=50, suffix="adjustment"):
		row = Ledger().get_valid_dict()
		row.update(self.target if values is None else values)
		row.update(
			name="LEDGER-1",
			owner="Administrator",
			modified="yesterday",
			idx=1,
			commission_amount=amount,
			commission_base=amount * 20,
			sync_key=f"Sales Invoice::INV-1::Sales Partner::partner::{suffix}",
			custom_sync_key=f"Sales Invoice::INV-1::Sales Partner::partner::{suffix}",
		)
		return row

	def run_sync(self, values=None):
		commission.apply_recalculated_commission_ledgers(
			self.invoice,
			[dict(self.target)] if values is None else values,
		)

	def assert_no_writes(self):
		self.deleted.assert_not_called()
		self.assertEqual(self.inserted, [])

	def test_unchanged_rows_keep_identity_and_skip_cleanup(self):
		self.rows = [self.stored()]
		self.run_sync()
		self.assert_no_writes()
		self.assertEqual(self.get_all.call_args.kwargs["filters"]["status"], ["!=", "Paid"])

	def test_database_number_date_and_empty_representations_match(self):
		self.rows = [self.stored()]
		self.rows[0].update(
			commission_amount=Decimal("50.000000000"),
			posting_date=date(2026, 9, 22),
			sales_person="",
			ineligibility_reason="",
		)
		self.run_sync()
		self.assert_no_writes()

	def test_changed_amount_is_rebuilt(self):
		self.rows = [self.stored(amount=40)]
		self.run_sync()
		self.deleted.assert_called_once_with("INV-1")
		self.assertEqual(self.inserted[0]["commission_amount"], 50)

	def test_sub_storage_precision_noise_does_not_rebuild(self):
		self.rows = [self.stored()]
		self.target["commission_amount"] = 50.000000000001
		self.run_sync()
		self.assert_no_writes()

	def test_change_at_storage_precision_is_not_ignored(self):
		self.rows = [self.stored()]
		self.target["commission_amount"] = 50.000000001
		self.run_sync()
		self.deleted.assert_called_once()

	def test_same_amount_changed_eligibility_metadata_is_rebuilt(self):
		self.rows = [self.stored()]
		self.rows[0]["ineligibility_reason"] = "Stale reason"
		self.run_sync()
		self.deleted.assert_called_once()
		self.assertIsNone(self.inserted[0]["ineligibility_reason"])

	def test_partial_payment_preserves_paid_amount_and_skips_unchanged_delta(self):
		self.paid = ({("Sales Partner", ""): 20}, {("Sales Partner", ""): dict(self.target)})
		self.rows = [self.stored(amount=30)]
		self.run_sync()
		self.assert_no_writes()

	def test_return_creates_negative_adjustment_without_touching_paid(self):
		self.paid = ({("Sales Partner", ""): 80}, {("Sales Partner", ""): dict(self.target)})
		self.run_sync()
		self.assertEqual(self.inserted[0]["commission_amount"], -30)
		self.assertEqual(self.inserted[0]["status"], "Unpaid")
		self.assertEqual(self.paid[0][("Sales Partner", "")], 80)

	def test_fully_paid_unchanged_invoice_has_no_unpaid_writes(self):
		self.paid = ({("Sales Partner", ""): 50}, {("Sales Partner", ""): dict(self.target)})
		self.run_sync()
		self.assert_no_writes()

	def test_ineligible_zero_amount_row_is_preserved(self):
		self.target.update(
			commission_amount=0,
			commission_base=0,
			eligibility_status="Ineligible",
			ineligibility_reason="Outstanding threshold exceeded",
		)
		self.rows = [self.stored(amount=0, suffix="ineligible")]
		self.run_sync()
		self.assert_no_writes()

	def test_changed_potential_amount_on_ineligible_row_is_rebuilt(self):
		self.target.update(commission_amount=0, commission_base=0, eligibility_status="Ineligible")
		self.rows = [self.stored(amount=0, suffix="ineligible")]
		self.rows[0]["potential_commission_amount"] = 40
		self.run_sync()
		self.deleted.assert_called_once()
		self.assertEqual(self.inserted[0]["potential_commission_amount"], 50)

	def test_cancelled_invoice_removes_stale_unpaid_rows(self):
		self.rows = [self.stored()]
		self.run_sync([])
		self.deleted.assert_called_once()
		self.assertEqual(self.inserted, [])

	def test_empty_result_already_empty_does_not_write(self):
		self.run_sync([])
		self.assert_no_writes()

	def test_duplicate_rows_are_rebuilt(self):
		self.rows = [self.stored(), self.stored()]
		self.run_sync()
		self.deleted.assert_called_once()
		self.assertEqual(len(self.inserted), 1)

	def test_missing_rows_are_inserted(self):
		self.run_sync()
		self.assertEqual(len(self.inserted), 1)
		self.assertEqual(self.inserted[0]["commission_amount"], 50)

	def test_row_order_is_irrelevant_but_duplicate_parties_are_not(self):
		other = dict(self.target, sales_person="Person B", commission_type="Sales Person")
		first, second = self.stored(), self.stored(other)
		first.pop("name")
		second.pop("name")
		self.rows = [second, first]
		self.assertTrue(commission.unpaid_commission_ledgers_match("INV-1", [first, second]))
		self.rows = [first, first]
		self.assertFalse(commission.unpaid_commission_ledgers_match("INV-1", [first, second]))


if __name__ == "__main__":
	unittest.main()
