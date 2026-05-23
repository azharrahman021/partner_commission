import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_settings_doctype()
    create_settings_fields()
    create_ledger_fields()
    frappe.clear_cache(doctype="Sales Commission Ledger")
    frappe.clear_cache(doctype="Partner Commission Settings")


def create_settings_doctype():
    if frappe.db.exists("DocType", "Partner Commission Settings"):
        return

    doc = frappe.get_doc(
        {
            "doctype": "DocType",
            "name": "Partner Commission Settings",
            "module": "Partner Commission",
            "custom": 1,
            "issingle": 1,
            "fields": [
                {
                    "fieldname": "outstanding_threshold",
                    "label": "Outstanding Threshold Across Companies",
                    "fieldtype": "Currency",
                    "description": (
                        "If any customer linked to a sales partner has total outstanding "
                        "above this amount across companies, partner and sales person "
                        "incentives are marked ineligible."
                    ),
                },
                {
                    "fieldname": "payment_grace_days",
                    "label": "Sales Person Payment Grace Days",
                    "fieldtype": "Int",
                    "description": (
                        "Sales person incentive is marked ineligible when the invoice is "
                        "not fully paid within this many days from posting date."
                    ),
                },
            ],
            "permissions": [
                {
                    "role": "System Manager",
                    "read": 1,
                    "write": 1,
                    "create": 1,
                    "delete": 1,
                }
            ],
        }
    )
    doc.insert(ignore_permissions=True)


def create_settings_fields():
    create_custom_fields(
        {
            "Partner Commission Settings": [
                {
                    "fieldname": "payment_outstanding_tolerance",
                    "label": "Payment Outstanding Tolerance",
                    "fieldtype": "Currency",
                    "default": 5,
                    "description": (
                        "Invoice commission stays eligible when the remaining outstanding "
                        "amount is within this tolerance after the grace period."
                    ),
                }
            ]
        }
    )


def create_ledger_fields():
    create_custom_fields(
        {
            "Sales Commission Ledger": [
                {
                    "fieldname": "eligibility_status",
                    "label": "Eligibility Status",
                    "fieldtype": "Select",
                    "options": "Eligible\nIneligible",
                    "default": "Eligible",
                    "insert_after": "status",
                    "read_only": 1,
                    "in_list_view": 1,
                },
                {
                    "fieldname": "ineligibility_reason",
                    "label": "Ineligibility Reason",
                    "fieldtype": "Small Text",
                    "insert_after": "eligibility_status",
                    "read_only": 1,
                },
                {
                    "fieldname": "outstanding_threshold",
                    "label": "Outstanding Threshold",
                    "fieldtype": "Currency",
                    "insert_after": "ineligibility_reason",
                    "read_only": 1,
                },
                {
                    "fieldname": "partner_outstanding_amount",
                    "label": "Partner Customer Outstanding",
                    "fieldtype": "Currency",
                    "insert_after": "outstanding_threshold",
                    "read_only": 1,
                },
                {
                    "fieldname": "payment_grace_days",
                    "label": "Payment Grace Days",
                    "fieldtype": "Int",
                    "insert_after": "partner_outstanding_amount",
                    "read_only": 1,
                },
                {
                    "fieldname": "payment_delay_days",
                    "label": "Payment Delay Days",
                    "fieldtype": "Int",
                    "insert_after": "payment_grace_days",
                    "read_only": 1,
                },
                {
                    "fieldname": "latest_payment_date",
                    "label": "Latest Payment Date",
                    "fieldtype": "Date",
                    "insert_after": "payment_delay_days",
                    "read_only": 1,
                },
            ]
        }
    )
