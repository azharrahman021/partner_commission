import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields(
        {
            "Sales Commission Ledger": [
                {
                    "fieldname": "potential_commission_amount",
                    "label": "Potential Commission Amount",
                    "fieldtype": "Currency",
                    "insert_after": "commission_amount",
                    "read_only": 1,
                }
            ]
        }
    )
    frappe.clear_cache(doctype="Sales Commission Ledger")
