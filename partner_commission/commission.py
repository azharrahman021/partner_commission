import frappe
from frappe.utils import add_to_date, flt, now_datetime


def sync_from_payment_entry(doc, method=None):
    invoices = get_invoices_from_payment_entry(doc)
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
        delete_ledgers_for_invoice(invoice.name)
        return

    if invoice.get("is_return"):
        return

    if not invoice.get("sales_partner"):
        delete_ledgers_for_invoice(invoice.name)
        return

    commission_rate = get_commission_rate(invoice)

    if commission_rate <= 0:
        delete_ledgers_for_invoice(invoice.name)
        return

    gross_invoice_total = flt(invoice.grand_total)
    net_invoice_total = flt(invoice.net_total)

    if gross_invoice_total <= 0 or net_invoice_total <= 0:
        delete_ledgers_for_invoice(invoice.name)
        return

    delete_ledgers_for_invoice(invoice.name)

    payment_rows = get_payment_realizations(invoice.name)
    journal_write_off = get_journal_write_off_amount(invoice.name)
    credit_note_total = get_credit_note_total(invoice.name)

    total_paid = sum(flt(row["allocated_amount"]) for row in payment_rows)

    total_realized_before_credit_note = total_paid - journal_write_off
    actual_realization = total_realized_before_credit_note - credit_note_total

    if actual_realization <= 0:
        return

    # Cap commission basis so overpayment or accounting oddities do not overpay commission.
    actual_realization = min(actual_realization, gross_invoice_total)

    ratio = actual_realization / gross_invoice_total
    commission_base = net_invoice_total * ratio
    commission_amount = commission_base * commission_rate / 100

    create_invoice_summary_ledger(
        invoice=invoice,
        gross_invoice_total=gross_invoice_total,
        net_invoice_total=net_invoice_total,
        allocated_amount=total_paid,
        write_off_allocated=journal_write_off,
        credit_note_amount=credit_note_total,
        commission_base=commission_base,
        commission_rate=commission_rate,
        commission_amount=commission_amount,
    )


def get_commission_rate(invoice):
    # Prefer the invoice's saved rate so old invoices do not change when partner master changes.
    if flt(invoice.get("commission_rate")):
        return flt(invoice.get("commission_rate"))

    return flt(
        frappe.db.get_value(
            "Sales Partner",
            invoice.sales_partner,
            "commission_rate",
        )
    )


def get_payment_realizations(invoice_name):
    return frappe.db.sql(
        """
        SELECT
            pe.name AS payment_entry,
            pe.posting_date,
            pe.company,
            per.allocated_amount,
            pe.write_off_amount
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


def create_invoice_summary_ledger(
    invoice,
    gross_invoice_total,
    net_invoice_total,
    allocated_amount,
    write_off_allocated,
    credit_note_amount,
    commission_base,
    commission_rate,
    commission_amount,
):
    ledger = frappe.new_doc("Sales Commission Ledger")

    ledger.sales_partner = invoice.sales_partner
    ledger.sales_invoice = invoice.name
    ledger.posting_date = invoice.posting_date
    ledger.company = invoice.company

    ledger.gross_invoice_total = gross_invoice_total
    ledger.net_invoice_total = net_invoice_total
    ledger.allocated_amount = allocated_amount
    ledger.write_off_allocated = write_off_allocated
    ledger.credit_note_amount = credit_note_amount
    ledger.commission_base = commission_base
    ledger.commission_rate = commission_rate
    ledger.commission_amount = commission_amount

    set_if_field_exists(ledger, "reference_doctype", "Sales Invoice")
    set_if_field_exists(ledger, "reference_name", invoice.name)
    set_if_field_exists(ledger, "sync_key", f"Sales Invoice::{invoice.name}")

    ledger.insert(ignore_permissions=True)


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

