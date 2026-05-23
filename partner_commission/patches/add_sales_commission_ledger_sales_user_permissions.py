import frappe


def execute():
    roles = ("Sales User", "POSNext Cashier")
    doctype = "Sales Commission Ledger"

    if not frappe.db.exists("DocType", doctype):
        return

    for role in roles:
        if not frappe.db.exists("Role", role):
            continue

        existing = frappe.db.get_value(
            "Custom DocPerm",
            {"parent": doctype, "role": role, "permlevel": 0},
            "name",
        )

        values = {
            "read": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "email": 1,
        }

        if existing:
            frappe.db.set_value(
                "Custom DocPerm",
                existing,
                values,
                update_modified=False,
            )
            continue

        frappe.get_doc(
            {
                "doctype": "Custom DocPerm",
                "parent": doctype,
                "parenttype": "DocType",
                "parentfield": "permissions",
                "role": role,
                "permlevel": 0,
                **values,
            }
        ).insert(ignore_permissions=True)

    frappe.clear_cache(doctype=doctype)
