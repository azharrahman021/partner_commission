import frappe
from frappe.utils import add_days, add_to_date, date_diff, flt, getdate, now_datetime


FIXED_COMMISSION_RATE = 5
DEFAULT_OUTSTANDING_THRESHOLD = 0
DEFAULT_PAYMENT_GRACE_DAYS = 0
DEFAULT_PAYMENT_OUTSTANDING_TOLERANCE = 5


def sync_from_payment_entry(doc, method=None):
    frappe.log_error(f"Hook fired for Payment Entry {doc.name}", "Partner Commission Debug")
    invoices = get_invoices_from_payment_entry(doc)
    frappe.log_error(f"Invoices found: {list(invoices)}", "Partner Commission Debug")
    sync_invoices(invoices)


def sync_from_journal_entry(doc, method=None):
    invoices = get_invoices_from_journal_entry(doc)
    sync_invoices(invoices)


def sync_from_sales_invoice(doc, method=None):
    invoices = {doc.name}

    if doc.get("is_return") and doc.get("return_against"):
        invoices.add(doc.return_against)

    sync_invoices(invoices)


def sync_recent_commissions():
    cutoff_time = add_to_date(now_datetime(), hours=-48)

    invoices = set()

    payment_entries = frappe.get_all(
        "Payment Entry",
        filters={"modified": [">=", cutoff_time]},
        pluck="name",
    )

    for pe_name in payment_entries:
        pe = frappe.get_doc("Payment Entry", pe_name)
        invoices.update(get_invoices_from_payment_entry(pe))

    journal_entries = frappe.get_all(
        "Journal Entry",
        filters={"modified": [">=", cutoff_time]},
        pluck="name",
    )

    for je_name in journal_entries:
        je = frappe.get_doc("Journal Entry", je_name)
        invoices.update(get_invoices_from_journal_entry(je))

    sales_invoices = frappe.get_all(
        "Sales Invoice",
        filters={"modified": [">=", cutoff_time]},
        fields=["name", "is_return", "return_against"],
    )

    for si in sales_invoices:
        if si.is_return and si.return_against:
            invoices.add(si.return_against)
        else:
            invoices.add(si.name)

    sync_invoices(invoices)


def rebuild_all_commission_ledgers(batch_size=500, from_date=None, to_date=None):
    filters = {"docstatus": 1, "is_return": 0}
    if from_date and to_date:
        filters["posting_date"] = ["between", [from_date, to_date]]
    elif from_date:
        filters["posting_date"] = [">=", from_date]
    elif to_date:
        filters["posting_date"] = ["<=", to_date]

    total = frappe.db.count("Sales Invoice", filters)
    processed = 0

    while processed < total:
        invoice_names = frappe.get_all(
            "Sales Invoice",
            filters=filters,
            pluck="name",
            order_by="posting_date asc, name asc",
            start=processed,
            page_length=batch_size,
        )

        if not invoice_names:
            break

        sync_invoices(invoice_names)
        processed += len(invoice_names)
        frappe.db.commit()

    return {
        "fixed_commission_rate": FIXED_COMMISSION_RATE,
        "settings": get_commission_settings(),
        "from_date": from_date,
        "to_date": to_date,
        "processed": processed,
        "total": total,
    }


def sync_invoices(invoice_names):
    for invoice_name in invoice_names:
        try:
            rebuild_invoice_commission(invoice_name)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Commission Sync Error: {invoice_name}",
            )


def rebuild_invoice_commission(invoice_name):
    if not invoice_name:
        return

    if not frappe.db.exists("Sales Invoice", invoice_name):
        return

    invoice = frappe.get_doc("Sales Invoice", invoice_name)

    if invoice.docstatus != 1:
        apply_recalculated_commission_ledgers(invoice, [])
        return

    if invoice.get("is_return"):
        return

    gross_invoice_total = flt(invoice.grand_total)
    net_invoice_total = flt(invoice.net_total)

    if gross_invoice_total <= 0 or net_invoice_total <= 0:
        apply_recalculated_commission_ledgers(invoice, [])
        return

    payment_rows = get_payment_realizations(invoice.name)
    payment_rows.extend(get_sales_invoice_payment_realizations(invoice))
    payment_deductions = get_payment_entry_deductions(invoice.name)
    journal_write_off = get_journal_write_off_amount(invoice.name)
    invoice_write_off = get_invoice_write_off_amount(invoice)
    total_write_off = payment_deductions + journal_write_off + invoice_write_off

    credit_note_total = get_credit_note_total(invoice.name)

    total_paid = sum(flt(row["allocated_amount"]) for row in payment_rows)
    latest_payment_date = get_latest_payment_date(payment_rows)
    total_realized_before_credit_note = total_paid - payment_deductions - journal_write_off
    actual_realization = total_realized_before_credit_note - credit_note_total

    # Cap realization so overpayment or accounting oddities do not overstate profit.
    actual_realization = min(actual_realization, gross_invoice_total)

    recorded_profit = flt(invoice.get("custom_profit"))
    realization_loss = max(gross_invoice_total - actual_realization, invoice_write_off, 0)
    commission_base = recorded_profit - realization_loss

    if commission_base <= 0:
        apply_recalculated_commission_ledgers(invoice, [])
        return

    settings = get_commission_settings()
    partner_outstanding_block = get_partner_outstanding_block(invoice.sales_partner, settings)
    payment_delay_block = get_invoice_payment_delay_block(invoice, latest_payment_date, settings)
    recalculated_ledgers = []

    if invoice.get("sales_partner"):
        commission_rate = get_commission_rate(invoice)
        potential_commission_amount = commission_base * commission_rate / 100
        block = partner_outstanding_block or payment_delay_block
        is_eligible = not block
        eligible_base = commission_base if is_eligible else 0
        commission_amount = eligible_base * commission_rate / 100

        if commission_rate > 0:
            recalculated_ledgers.append(
                get_invoice_summary_ledger_values(
                    invoice=invoice,
                    commission_type="Sales Partner",
                    sales_person=None,
                    gross_invoice_total=gross_invoice_total,
                    net_invoice_total=net_invoice_total,
                    allocated_amount=total_paid,
                    write_off_allocated=total_write_off,
                    credit_note_amount=credit_note_total,
                    actual_realization=actual_realization,
                    commission_base=eligible_base,
                    commission_rate=commission_rate,
                    commission_amount=commission_amount,
                    potential_commission_amount=potential_commission_amount,
                    eligibility_status="Eligible" if is_eligible else "Ineligible",
                    ineligibility_reason=get_block_reason(block),
                    outstanding_threshold=settings["outstanding_threshold"],
                    partner_outstanding_amount=get_block_amount(partner_outstanding_block),
                    payment_grace_days=settings["payment_grace_days"],
                    payment_outstanding_tolerance=settings["payment_outstanding_tolerance"],
                    payment_delay_days=get_block_delay_days(payment_delay_block),
                    latest_payment_date=latest_payment_date,
                )
            )

    for sales_person_commission in get_sales_person_commissions(invoice, commission_base):
        block = partner_outstanding_block or payment_delay_block
        is_eligible = not block
        eligible_base = sales_person_commission["commission_base"] if is_eligible else 0
        commission_rate = sales_person_commission["commission_rate"]
        potential_commission_amount = sales_person_commission["commission_base"] * commission_rate / 100
        commission_amount = eligible_base * commission_rate / 100

        recalculated_ledgers.append(
            get_invoice_summary_ledger_values(
                invoice=invoice,
                commission_type="Sales Person",
                sales_person=sales_person_commission["sales_person"],
                gross_invoice_total=gross_invoice_total,
                net_invoice_total=net_invoice_total,
                allocated_amount=0,
                write_off_allocated=0,
                credit_note_amount=0,
                actual_realization=0,
                commission_base=eligible_base,
                commission_rate=commission_rate,
                commission_amount=commission_amount,
                potential_commission_amount=potential_commission_amount,
                eligibility_status="Eligible" if is_eligible else "Ineligible",
                ineligibility_reason=get_block_reason(block),
                outstanding_threshold=settings["outstanding_threshold"],
                partner_outstanding_amount=get_block_amount(partner_outstanding_block),
                payment_grace_days=settings["payment_grace_days"],
                payment_outstanding_tolerance=settings["payment_outstanding_tolerance"],
                payment_delay_days=get_block_delay_days(payment_delay_block),
                latest_payment_date=latest_payment_date,
            )
        )

    apply_recalculated_commission_ledgers(invoice, recalculated_ledgers)


def get_commission_rate(invoice):
    return FIXED_COMMISSION_RATE


def get_payment_realizations(invoice_name):
    return frappe.db.sql(
        """
        SELECT
            pe.name AS payment_entry,
            pe.posting_date,
            pe.company,
            per.allocated_amount
        FROM
            `tabPayment Entry Reference` per
        INNER JOIN
            `tabPayment Entry` pe
        ON
            pe.name = per.parent
        WHERE
            pe.docstatus = 1
            AND per.reference_doctype = 'Sales Invoice'
            AND per.reference_name = %s
            AND IFNULL(per.allocated_amount, 0) > 0
        """,
        invoice_name,
        as_dict=True,
    )


def get_sales_invoice_payment_realizations(invoice):
    if not invoice.get("payments"):
        return []

    return [
        {
            "payment_entry": None,
            "posting_date": invoice.posting_date,
            "company": invoice.company,
            "allocated_amount": flt(payment.amount),
        }
        for payment in invoice.get("payments", [])
        if flt(payment.amount) > 0
    ]


def get_latest_payment_date(payment_rows):
    dates = [getdate(row.get("posting_date")) for row in payment_rows if row.get("posting_date")]
    return max(dates) if dates else None


def get_payment_entry_deductions(invoice_name):
    return flt(
        frappe.db.sql(
            """
            SELECT
                SUM(ABS(IFNULL(ped.amount, 0)))
            FROM
                `tabPayment Entry Deduction` ped
            INNER JOIN
                `tabPayment Entry` pe
            ON
                pe.name = ped.parent
            INNER JOIN
                `tabPayment Entry Reference` per
            ON
                per.parent = pe.name
            WHERE
                pe.docstatus = 1
                AND per.reference_doctype = 'Sales Invoice'
                AND per.reference_name = %s
            """,
            invoice_name,
        )[0][0]
        or 0
    )


def get_journal_write_off_amount(invoice_name):
    # This catches Journal Entries allocated directly against the Sales Invoice.
    # Common ERPNext fields are reference_type/reference_name on Journal Entry Account.
    amount = frappe.db.sql(
        """
        SELECT
            SUM(ABS(IFNULL(jea.debit_in_account_currency, 0) - IFNULL(jea.credit_in_account_currency, 0)))
        FROM
            `tabJournal Entry Account` jea
        INNER JOIN
            `tabJournal Entry` je
        ON
            je.name = jea.parent
        WHERE
            je.docstatus = 1
            AND jea.reference_type = 'Sales Invoice'
            AND jea.reference_name = %s
        """,
        invoice_name,
    )

    return flt(amount[0][0]) if amount and amount[0] else 0


def get_invoice_write_off_amount(invoice):
    return abs(flt(invoice.get("base_write_off_amount") or invoice.get("write_off_amount")))


def get_credit_note_total(invoice_name):
    credit_notes = frappe.get_all(
        "Sales Invoice",
        filters={
            "is_return": 1,
            "return_against": invoice_name,
            "docstatus": 1,
        },
        fields=["grand_total"],
    )

    return sum(abs(flt(row.grand_total)) for row in credit_notes)


def get_sales_person_commissions(invoice, commission_base):
    sales_people = {}

    for row in invoice.get("sales_team", []):
        sales_person = row.get("sales_person") or row.get("salesperson")
        if not sales_person:
            continue

        allocated_percentage = flt(row.get("allocated_percentage"))
        sales_people.setdefault(
            sales_person,
            {
                "allocated_percentage": 0,
                "commission_amount": 0,
                "commission_base": 0,
            },
        )
        sales_people[sales_person]["allocated_percentage"] += allocated_percentage

    positive_total = sum(
        sales_person["allocated_percentage"]
        for sales_person in sales_people.values()
        if sales_person["allocated_percentage"] > 0
    )

    for row in invoice.get("sales_team", []):
        sales_person = row.get("sales_person") or row.get("salesperson")
        if not sales_person:
            continue

        allocated_percentage = flt(row.get("allocated_percentage"))
        if positive_total > 0:
            allocation_share = allocated_percentage / positive_total if allocated_percentage > 0 else 0
        else:
            allocation_share = 1 / len(sales_people) if sales_people else 0

        row_commission_base = commission_base * allocation_share
        commission_rate = FIXED_COMMISSION_RATE
        commission_amount = row_commission_base * commission_rate / 100

        sales_people[sales_person]["commission_amount"] += commission_amount
        sales_people[sales_person]["commission_base"] += row_commission_base

    commissions = []
    for sales_person, values in sales_people.items():
        if flt(values["commission_amount"]) == 0:
            continue

        commission_rate = (
            values["commission_amount"] * 100 / values["commission_base"]
            if values["commission_base"]
            else 0
        )

        commissions.append(
            {
                "sales_person": sales_person,
                "commission_base": values["commission_base"],
                "commission_rate": commission_rate,
                "commission_amount": values["commission_amount"],
            }
        )

    return commissions


def get_commission_settings():
    settings = {
        "outstanding_threshold": DEFAULT_OUTSTANDING_THRESHOLD,
        "payment_grace_days": DEFAULT_PAYMENT_GRACE_DAYS,
        "payment_outstanding_tolerance": DEFAULT_PAYMENT_OUTSTANDING_TOLERANCE,
    }

    if not frappe.db.exists("DocType", "Partner Commission Settings"):
        return settings

    try:
        doc = frappe.get_single("Partner Commission Settings")
    except Exception:
        return settings

    settings["outstanding_threshold"] = flt(doc.get("outstanding_threshold"))
    settings["payment_grace_days"] = int(flt(doc.get("payment_grace_days")))

    tolerance = doc.get("payment_outstanding_tolerance")
    settings["payment_outstanding_tolerance"] = (
        DEFAULT_PAYMENT_OUTSTANDING_TOLERANCE
        if tolerance in (None, "")
        else flt(tolerance)
    )
    return settings


def get_partner_outstanding_block(sales_partner, settings=None):
    if not sales_partner:
        return None

    settings = settings or get_commission_settings()
    threshold = flt(settings.get("outstanding_threshold"))
    if threshold <= 0:
        return None

    row = frappe.db.sql(
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
        LIMIT 1
        """,
        {"sales_partner": sales_partner, "threshold": threshold},
        as_dict=True,
    )

    if not row:
        return None

    return {
        "rule": "Partner Outstanding Threshold",
        "customer": row[0].customer,
        "customer_name": row[0].customer_name,
        "amount": flt(row[0].outstanding_amount),
        "threshold": threshold,
        "reason": (
            f"{row[0].customer_name or row[0].customer} has outstanding "
            f"{flt(row[0].outstanding_amount)} across companies, above threshold {threshold}."
        ),
    }


def get_invoice_payment_delay_block(invoice, latest_payment_date, settings=None):
    settings = settings or get_commission_settings()
    grace_days = int(flt(settings.get("payment_grace_days")))
    tolerance = flt(settings.get("payment_outstanding_tolerance"))
    if grace_days <= 0:
        return None

    due_by = add_days(invoice.posting_date, grace_days)
    outstanding_amount = flt(invoice.get("outstanding_amount"))

    if outstanding_amount > tolerance and getdate() > getdate(due_by):
        return {
            "rule": "Invoice Payment Delay",
            "delay_days": date_diff(getdate(), due_by),
            "reason": (
                f"Invoice is not fully paid within {grace_days} days; "
                f"current outstanding is {outstanding_amount}, above tolerance {tolerance}."
            ),
        }

    if latest_payment_date and getdate(latest_payment_date) > getdate(due_by):
        return {
            "rule": "Invoice Payment Delay",
            "delay_days": date_diff(latest_payment_date, due_by),
            "reason": f"Invoice payment was completed after the {grace_days}-day limit.",
        }

    return None


def get_block_reason(block):
    return block.get("reason") if block else None


def get_block_amount(block):
    return flt(block.get("amount")) if block else 0


def get_block_delay_days(block):
    return int(flt(block.get("delay_days"))) if block else 0


def create_invoice_summary_ledger(
    invoice,
    commission_type,
    sales_person,
    gross_invoice_total,
    net_invoice_total,
    allocated_amount,
    write_off_allocated,
    credit_note_amount,
    actual_realization,
    commission_base,
    commission_rate,
    commission_amount,
    potential_commission_amount=None,
    eligibility_status="Eligible",
    ineligibility_reason=None,
    outstanding_threshold=0,
    partner_outstanding_amount=0,
    payment_grace_days=0,
    payment_outstanding_tolerance=0,
    payment_delay_days=0,
    latest_payment_date=None,
    sync_key_suffix=None,
):
    values = get_invoice_summary_ledger_values(
        invoice=invoice,
        commission_type=commission_type,
        sales_person=sales_person,
        gross_invoice_total=gross_invoice_total,
        net_invoice_total=net_invoice_total,
        allocated_amount=allocated_amount,
        write_off_allocated=write_off_allocated,
        credit_note_amount=credit_note_amount,
        actual_realization=actual_realization,
        commission_base=commission_base,
        commission_rate=commission_rate,
        commission_amount=commission_amount,
        potential_commission_amount=potential_commission_amount,
        eligibility_status=eligibility_status,
        ineligibility_reason=ineligibility_reason,
        outstanding_threshold=outstanding_threshold,
        partner_outstanding_amount=partner_outstanding_amount,
        payment_grace_days=payment_grace_days,
        payment_outstanding_tolerance=payment_outstanding_tolerance,
        payment_delay_days=payment_delay_days,
        latest_payment_date=latest_payment_date,
        sync_key_suffix=sync_key_suffix,
    )
    insert_commission_ledger(values)


def get_invoice_summary_ledger_values(
    invoice,
    commission_type,
    sales_person,
    gross_invoice_total,
    net_invoice_total,
    allocated_amount,
    write_off_allocated,
    credit_note_amount,
    actual_realization,
    commission_base,
    commission_rate,
    commission_amount,
    potential_commission_amount=None,
    eligibility_status="Eligible",
    ineligibility_reason=None,
    outstanding_threshold=0,
    partner_outstanding_amount=0,
    payment_grace_days=0,
    payment_outstanding_tolerance=0,
    payment_delay_days=0,
    latest_payment_date=None,
    sync_key_suffix=None,
):
    sync_key = f"Sales Invoice::{invoice.name}::{commission_type}::{sales_person or 'partner'}"
    if sync_key_suffix:
        sync_key = f"{sync_key}::{sync_key_suffix}"

    return {
        "sales_partner": invoice.sales_partner,
        "sales_person": sales_person,
        "commission_type": commission_type,
        "sales_invoice": invoice.name,
        "posting_date": invoice.posting_date,
        "company": invoice.company,
        "gross_invoice_total": gross_invoice_total,
        "net_invoice_total": net_invoice_total,
        "allocated_amount": allocated_amount,
        "write_off_allocated": write_off_allocated,
        "credit_note_amount": credit_note_amount,
        "commission_base": commission_base,
        "net_realization": actual_realization,
        "commission_rate": commission_rate,
        "commission_amount": commission_amount,
        "potential_commission_amount": (
            flt(potential_commission_amount)
            if potential_commission_amount is not None
            else flt(commission_amount)
        ),
        "status": "Unpaid",
        "eligibility_status": eligibility_status,
        "ineligibility_reason": ineligibility_reason,
        "outstanding_threshold": outstanding_threshold,
        "partner_outstanding_amount": partner_outstanding_amount,
        "payment_grace_days": payment_grace_days,
        "payment_outstanding_tolerance": payment_outstanding_tolerance,
        "payment_delay_days": payment_delay_days,
        "latest_payment_date": latest_payment_date,
        "reference_doctype": "Sales Invoice",
        "reference_name": invoice.name,
        "sync_key": sync_key,
        "custom_reference_doctype": "Sales Invoice",
        "custom_reference_name": invoice.name,
        "custom_sync_key": sync_key,
    }


def insert_commission_ledger(values):
    ledger = frappe.new_doc("Sales Commission Ledger")

    for fieldname, value in values.items():
        set_if_field_exists(ledger, fieldname, value)

    ledger.insert(ignore_permissions=True, ignore_mandatory=True)


def apply_recalculated_commission_ledgers(invoice, recalculated_ledgers):
    paid_totals, paid_metadata = get_paid_commission_totals(invoice.name)
    target_totals = {}
    target_metadata = {}

    for values in recalculated_ledgers:
        key = get_commission_party_key(values)
        target_totals[key] = target_totals.get(key, 0) + flt(values.get("commission_amount"))
        target_metadata.setdefault(key, values)

    delete_unpaid_ledgers_for_invoice(invoice.name)

    for key in set(target_totals) | set(paid_totals):
        target_amount = flt(target_totals.get(key))
        paid_amount = flt(paid_totals.get(key))
        delta_amount = target_amount - paid_amount
        values = dict(target_metadata.get(key) or paid_metadata.get(key) or {})

        if "potential_commission_amount" not in values:
            values["potential_commission_amount"] = flt(values.get("commission_amount"))

        if abs(delta_amount) < 0.0001:
            if (
                values
                and not paid_amount
                and values.get("eligibility_status") == "Ineligible"
            ):
                values["sync_key"] = get_adjustment_sync_key(values, "ineligible")
                values["custom_sync_key"] = values["sync_key"]
                insert_commission_ledger(values)
            continue

        if not values:
            commission_type, sales_person = key
            values = get_invoice_summary_ledger_values(
                invoice=invoice,
                commission_type=commission_type,
                sales_person=sales_person or None,
                gross_invoice_total=flt(invoice.grand_total),
                net_invoice_total=flt(invoice.net_total),
                allocated_amount=0,
                write_off_allocated=0,
                credit_note_amount=0,
                actual_realization=0,
                commission_base=0,
                commission_rate=0,
                commission_amount=0,
                potential_commission_amount=0,
                ineligibility_reason="Paid commission adjusted after recalculation.",
                sync_key_suffix="adjustment",
            )

        values["commission_amount"] = delta_amount
        values["commission_base"] = get_delta_commission_base(
            delta_amount,
            values.get("commission_rate"),
        )
        values["status"] = "Unpaid"
        values["sync_key"] = get_adjustment_sync_key(values, "adjustment")
        values["custom_sync_key"] = values["sync_key"]

        if delta_amount < 0 and not values.get("ineligibility_reason"):
            values["ineligibility_reason"] = "Paid commission adjusted after return or recalculation."

        insert_commission_ledger(values)


def get_paid_commission_totals(invoice_name):
    fields = [
        "sales_partner",
        "sales_person",
        "commission_type",
        "posting_date",
        "sales_invoice",
        "net_realization",
        "commission_rate",
        "commission_amount",
        "status",
        "company",
    ]
    for fieldname in (
        "eligibility_status",
        "ineligibility_reason",
        "outstanding_threshold",
        "partner_outstanding_amount",
        "payment_grace_days",
        "payment_outstanding_tolerance",
        "payment_delay_days",
        "latest_payment_date",
        "potential_commission_amount",
    ):
        if frappe.db.has_column("Sales Commission Ledger", fieldname):
            fields.append(fieldname)

    rows = frappe.get_all(
        "Sales Commission Ledger",
        filters={
            "sales_invoice": invoice_name,
            "status": "Paid",
        },
        fields=fields,
    )

    totals = {}
    metadata = {}
    for row in rows:
        key = get_commission_party_key(row)
        totals[key] = totals.get(key, 0) + flt(row.commission_amount)
        metadata.setdefault(key, dict(row))

    return totals, metadata


def get_commission_party_key(values):
    return (
        values.get("commission_type"),
        values.get("sales_person") or "",
    )


def get_delta_commission_base(delta_amount, commission_rate):
    commission_rate = flt(commission_rate)
    if not commission_rate:
        return 0

    return delta_amount * 100 / commission_rate


def get_adjustment_sync_key(values, suffix):
    return "Sales Invoice::{0}::{1}::{2}::{3}".format(
        values.get("sales_invoice"),
        values.get("commission_type"),
        values.get("sales_person") or "partner",
        suffix,
    )


def delete_ledgers_for_invoice(invoice_name):
    ledgers = frappe.get_all(
        "Sales Commission Ledger",
        filters={"sales_invoice": invoice_name},
        pluck="name",
    )

    for ledger_name in ledgers:
        frappe.delete_doc(
            "Sales Commission Ledger",
            ledger_name,
            ignore_permissions=True,
            force=True,
        )


def delete_unpaid_ledgers_for_invoice(invoice_name):
    ledgers = frappe.get_all(
        "Sales Commission Ledger",
        filters={
            "sales_invoice": invoice_name,
            "status": ["!=", "Paid"],
        },
        pluck="name",
    )

    for ledger_name in ledgers:
        frappe.delete_doc(
            "Sales Commission Ledger",
            ledger_name,
            ignore_permissions=True,
            force=True,
        )


def get_invoices_from_payment_entry(doc):
    invoices = set()

    for ref in doc.get("references", []):
        if ref.reference_doctype == "Sales Invoice" and ref.reference_name:
            invoices.add(ref.reference_name)

    return invoices


def get_invoices_from_journal_entry(doc):
    invoices = set()

    for account in doc.get("accounts", []):
        if account.reference_type == "Sales Invoice" and account.reference_name:
            invoices.add(account.reference_name)

    return invoices


def set_if_field_exists(doc, fieldname, value):
    if doc.meta.has_field(fieldname):
        doc.set(fieldname, value)


def set_sales_person_if_valid(doc, sales_partner):
    if not doc.meta.has_field("sales_person") or not sales_partner:
        return

    if frappe.db.exists("Sales Person", sales_partner):
        doc.sales_person = sales_partner
