import unittest
from types import SimpleNamespace

from partner_commission.api import summarize_commission_rows


class TestCommissionSummary(unittest.TestCase):
    def test_blocked_rows_use_potential_amount(self):
        rows = [
            SimpleNamespace(
                count=1,
                net_realization=1000,
                commission_amount=0,
                potential_commission_amount=100,
                status="Unpaid",
                eligibility_status="Ineligible",
            ),
            SimpleNamespace(
                count=2,
                net_realization=2000,
                commission_amount=50,
                potential_commission_amount=50,
                status="Paid",
                eligibility_status="Eligible",
            ),
        ]

        totals = summarize_commission_rows(rows)

        self.assertEqual(totals["total_entries"], 3)
        self.assertEqual(totals["total_net_realization"], 3000)
        self.assertEqual(totals["total_commission"], 50)
        self.assertEqual(totals["paid_commission"], 50)
        self.assertEqual(totals["unpaid_commission"], 0)
        self.assertEqual(totals["ineligible_entries"], 1)
        self.assertEqual(totals["blocked_potential_commission_total"], 100)

    def test_missing_potential_falls_back_to_commission_amount(self):
        rows = [
            SimpleNamespace(
                count=1,
                net_realization=500,
                commission_amount=0,
                status="Unpaid",
                eligibility_status="Ineligible",
            )
        ]

        totals = summarize_commission_rows(rows)

        self.assertEqual(totals["blocked_potential_commission_total"], 0)

