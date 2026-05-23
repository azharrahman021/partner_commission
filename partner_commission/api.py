import frappe
from frappe import _
from frappe.utils import add_months, flt, getdate, nowdate

from partner_commission.commission import get_commission_settings, get_partner_outstanding_block


def get_current_commission_party():
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    sales_partner = frappe.db.get_value(
        "Sales Partner",
        {"custom_portal_user": frappe.session.user},
        "name",
    )

    if sales_partner:
        return {
            "party_type": "Sales Partner",
            "field": "sales_partner",
            "value": sales_partner,
            "sales_partner": sales_partner,
            "sales_person": None,
        }

    mapped_sales_person = get_user_mapped_sales_person(frappe.session.user)
    if mapped_sales_person:
        return get_sales_person_party(mapped_sales_person)

    sales_person = frappe.db.sql(
        """
        SELECT sp.name
        FROM `tabSales Person` sp
        INNER JOIN `tabEmployee` e ON e.name = sp.employee
        WHERE e.user_id = %(user)s
            AND IFNULL(sp.enabled, 1) = 1
            AND IFNULL(sp.is_group, 0) = 0
        LIMIT 1
        """,
        {"user": frappe.session.user},
    )

    if sales_person:
        return get_sales_person_party(sales_person[0][0])

    if frappe.db.has_column("Sales Person", "custom_portal_user"):
        sales_person = frappe.db.get_value(
            "Sales Person",
            {
                "custom_portal_user": frappe.session.user,
                "enabled": 1,
                "is_group": 0,
            },
            "name",
        )

        if sales_person:
            return get_sales_person_party(sales_person)

    frappe.throw(
        _("No Sales Partner or Sales Person is linked to this user."),
        frappe.PermissionError,
    )


def get_sales_person_party(sales_person):
    return {
        "party_type": "Sales Person",
        "field": "sales_person",
        "value": sales_person,
        "sales_partner": None,
        "sales_person": sales_person,
    }


def get_user_mapped_sales_person(user):
    if not frappe.db.exists("DocType", "User Salesperson Mapping"):
        return None

    sales_person = frappe.db.get_value(
        "User Salesperson Mapping",
        {"user": user},
        "salesperson",
    )

    if not sales_person:
        return None

    if not frappe.db.exists(
        "Sales Person",
        {
            "name": sales_person,
            "enabled": 1,
            "is_group": 0,
        },
    ):
        return None

    return sales_person


def get_current_sales_partner():
    party = get_current_commission_party()

    if party["party_type"] != "Sales Partner":
        frappe.throw(_("No Sales Partner is linked to this user."), frappe.PermissionError)

    return party["sales_partner"]


@frappe.whitelist()
def get_my_partner():
    party = get_current_commission_party()

    if party["party_type"] == "Sales Partner":
        partner = frappe.db.get_value(
            "Sales Partner",
            party["sales_partner"],
            ["name", "partner_name", "commission_rate"],
            as_dict=True,
        )
        partner["party_type"] = party["party_type"]
        return partner

    partner = frappe.db.get_value(
        "Sales Person",
        party["sales_person"],
        ["name", "sales_person_name", "commission_rate", "employee"],
        as_dict=True,
    )
    partner["party_type"] = party["party_type"]

    return partner


@frappe.whitelist()
def get_commission_summary(from_date=None, to_date=None, status=None):
    party = get_current_commission_party()
    from_date, to_date = get_date_range(from_date, to_date)
    settings = get_commission_settings()
    outstanding_block = get_partner_outstanding_block(party["sales_partner"], settings)
    rows = get_commission_summary_rows(party, from_date, to_date, status=status)
    totals = summarize_commission_rows(rows)
    totals.update(
        {
            "party_type": party["party_type"],
            "sales_partner": party["sales_partner"],
            "sales_person": party["sales_person"],
            "from_date": from_date,
            "to_date": to_date,
            "settings": settings,
            "outstanding_block": outstanding_block,
            "by_status": rows,
        }
    )
    return totals


@frappe.whitelist()
def get_commission_ledger(from_date=None, to_date=None, status=None, limit_start=0, limit_page_length=50):
    party = get_current_commission_party()
    party_field = party["field"]
    from_date, to_date = get_date_range(from_date, to_date)

    filters = {
        "party_value": party["value"],
        "from_date": from_date,
        "to_date": to_date,
    }

    status_condition = ""
    if status:
        status_condition = "AND status = %(status)s"
        filters["status"] = status

    rows = frappe.db.sql(
        f"""
        SELECT
            name,
            posting_date,
            sales_invoice,
            sales_person,
            commission_type,
            net_realization,
            commission_rate,
            commission_amount,
            {get_potential_commission_select()},
            status,
            company,
            IFNULL(eligibility_status, 'Eligible') AS eligibility_status,
            ineligibility_reason,
            outstanding_threshold,
            partner_outstanding_amount,
            payment_grace_days,
            payment_delay_days,
            latest_payment_date
        FROM `tabSales Commission Ledger`
        WHERE {party_field} = %(party_value)s
            AND posting_date BETWEEN %(from_date)s AND %(to_date)s
            {status_condition}
        ORDER BY posting_date DESC, modified DESC
        LIMIT %(limit_start)s, %(limit_page_length)s
        """,
        {
            **filters,
            "limit_start": int(limit_start or 0),
            "limit_page_length": min(int(limit_page_length or 50), 100),
        },
        as_dict=True,
    )

    summary = get_commission_summary_data(party, from_date, to_date, status=status)

    return {
        "rows": rows,
        "summary": summary,
        "limit_start": int(limit_start or 0),
        "limit_page_length": min(int(limit_page_length or 50), 100),
    }


@frappe.whitelist()
def get_eligibility_settings():
    party = get_current_commission_party()
    settings = get_commission_settings()
    return {
        "settings": settings,
        "outstanding_block": get_partner_outstanding_block(party["sales_partner"], settings),
    }


@frappe.whitelist()
def get_partner_outstanding_details():
    party = get_current_commission_party()
    settings = get_commission_settings()
    threshold = flt(settings.get("outstanding_threshold"))

    if party["party_type"] != "Sales Partner" or threshold <= 0:
        return {
            "settings": settings,
            "blocked": False,
            "customers": [],
        }

    customers = frappe.db.sql(
        """
        SELECT
            customer,
            customer_name,
            SUM(IFNULL(outstanding_amount, 0)) AS outstanding_amount
        FROM `tabSales Invoice`
        WHERE docstatus = 1
            AND is_return = 0
            AND customer IN (
                SELECT DISTINCT customer
                FROM `tabSales Invoice`
                WHERE docstatus = 1
                    AND is_return = 0
                    AND sales_partner = %(sales_partner)s
                    AND IFNULL(customer, '') != ''
            )
        GROUP BY customer, customer_name
        HAVING SUM(IFNULL(outstanding_amount, 0)) > %(threshold)s
        ORDER BY outstanding_amount DESC
        """,
        {"sales_partner": sales_partner, "threshold": threshold},
        as_dict=True,
    )

    return {
        "settings": settings,
        "blocked": bool(customers),
        "customers": customers,
    }


def get_date_range(from_date=None, to_date=None):
    to_date = getdate(to_date or nowdate())
    from_date = getdate(from_date or add_months(to_date, -3))

    if from_date > to_date:
        frappe.throw(_("From Date cannot be after To Date"))

    return from_date, to_date


def get_commission_summary_rows(party, from_date, to_date, status=None):
    party_field = party["field"]
    filters = {
        "party_value": party["value"],
        "from_date": from_date,
        "to_date": to_date,
    }

    status_condition = ""
    if status:
        status_condition = "AND status = %(status)s"
        filters["status"] = status

    return frappe.db.sql(
        f"""
        SELECT
            status,
            IFNULL(eligibility_status, 'Eligible') AS eligibility_status,
            COUNT(*) AS count,
            SUM(IFNULL(net_realization, 0)) AS net_realization,
            SUM(IFNULL(commission_amount, 0)) AS commission_amount,
            SUM(IFNULL({get_potential_commission_sum_expr()}, IFNULL(commission_amount, 0))) AS potential_commission_amount
        FROM `tabSales Commission Ledger`
        WHERE {party_field} = %(party_value)s
            AND posting_date BETWEEN %(from_date)s AND %(to_date)s
            {status_condition}
        GROUP BY status, IFNULL(eligibility_status, 'Eligible')
        """,
        filters,
        as_dict=True,
    )


def get_commission_summary_data(party, from_date, to_date, status=None):
    settings = get_commission_settings()
    outstanding_block = get_partner_outstanding_block(party["sales_partner"], settings)
    rows = get_commission_summary_rows(party, from_date, to_date, status=status)
    totals = summarize_commission_rows(rows)
    totals.update(
        {
            "party_type": party["party_type"],
            "sales_partner": party["sales_partner"],
            "sales_person": party["sales_person"],
            "from_date": from_date,
            "to_date": to_date,
            "settings": settings,
            "outstanding_block": outstanding_block,
            "by_status": rows,
        }
    )
    return totals


def get_potential_commission_select():
    if frappe.db.has_column("Sales Commission Ledger", "potential_commission_amount"):
        return "IFNULL(potential_commission_amount, commission_amount) AS potential_commission_amount"

    return "commission_amount AS potential_commission_amount"


def get_potential_commission_sum_expr():
    if frappe.db.has_column("Sales Commission Ledger", "potential_commission_amount"):
        return "potential_commission_amount"

    return "commission_amount"


def summarize_commission_rows(rows):
    totals = {
        "total_entries": 0,
        "total_net_realization": 0,
        "total_commission": 0,
        "paid_commission": 0,
        "unpaid_commission": 0,
        "ineligible_entries": 0,
        "blocked_potential_commission_total": 0,
    }

    for row in rows:
        commission_amount = flt(getattr(row, "commission_amount", 0))
        potential_commission_amount = flt(
            getattr(row, "potential_commission_amount", commission_amount)
        )

        totals["total_entries"] += flt(getattr(row, "count", 0))
        totals["total_net_realization"] += flt(getattr(row, "net_realization", 0))
        totals["total_commission"] += commission_amount

        if getattr(row, "eligibility_status", "Eligible") == "Ineligible":
            totals["ineligible_entries"] += flt(getattr(row, "count", 0))
            totals["blocked_potential_commission_total"] += potential_commission_amount
        elif getattr(row, "status", None) == "Paid":
            totals["paid_commission"] += commission_amount
        else:
            totals["unpaid_commission"] += commission_amount

    return totals
