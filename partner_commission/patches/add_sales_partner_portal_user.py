import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields(
        {
            "Sales Partner": [
                {
                    "fieldname": "custom_portal_user",
                    "label": "Portal User",
                    "fieldtype": "Link",
                    "options": "User",
                    "insert_after": "commission_rate",
                    "unique": 1,
                    "description": "User account allowed to view this partner's commission in the partner app.",
                }
            ]
        },
        ignore_validate=True,
    )

    frappe.clear_cache(doctype="Sales Partner")
