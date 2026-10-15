from django.urls import path
from finance import (views, views_bank, views_billing, views_expenses, views_export,
                     views_recovery, views_shop, views_tax)

app_name = "finance"

urlpatterns = [
    # Parent
    path("wallet/",                           views.parent_wallet,          name="parent_wallet"),
    path("wallet/topup/",                     views.request_topup,          name="request_topup"),
    path("wallet/pay-fee/",                   views.pay_fee,                name="pay_fee"),
    path("wallet/buy/",                       views.buy_item,               name="buy_item"),
    path("wallet/submit-payment/",            views.submit_payment,         name="submit_payment"),
    path("wallet/pay-hostel/",                   views.parent_pay_hostel,      name="parent_pay_hostel"),
    path("wallet/history/",                   views.transaction_history,    name="transaction_history"),
    path("wallet/balance/",                   views.wallet_balance_api,     name="wallet_balance_api"),
    path("receipt/<int:txn_id>/",             views.receipt_txn,            name="receipt_txn"),
    path("receipt/payment/<int:payment_id>/", views.receipt_payment,        name="receipt_payment"),
    path("invoice/<int:invoice_id>/",         views.view_invoice,           name="view_invoice"),
    # Paystack
    path("paystack/init/",                    views.paystack_init,          name="paystack_init"),
    path("paystack/callback/",                views.paystack_callback,      name="paystack_callback"),
    path("paystack/webhook/",                 views.paystack_webhook,       name="paystack_webhook"),
    # Admin
    path("admin/",                                views.admin_finance_dashboard, name="admin_dashboard"),
    path("admin/payment/<int:payment_id>/review/",views.review_payment,         name="review_payment"),
    path("admin/topup/<int:topup_id>/review/",    views.review_topup,           name="review_topup"),
    path("admin/wallet/",                         views.admin_student_wallet,   name="admin_student_wallet"),
    path("admin/wallet/<int:student_id>/",        views.admin_student_wallet,   name="admin_student_wallet_id"),
    path("admin/wallet/adjust/",                  views.admin_adjust_wallet,    name="admin_adjust_wallet"),
    path("admin/invoices/",                       views.admin_invoices,         name="admin_invoices"),
    path("admin/billing/",                        views.admin_billing_report,   name="admin_billing"),
    path("admin/fees/",                           views.manage_fees,            name="manage_fees"),
    path("admin/fees/waive/",                     views.waive_fee,              name="waive_fee"),
    path("admin/items/",                          views.manage_items,           name="manage_items"),
    path("admin/audit/",                          views.admin_audit_log,        name="admin_audit_log"),

    # Discounts, plans and debtor follow-up
    path("admin/discounts/",            views_billing.discounts,          name="discounts"),
    path("admin/discounts/preview/",    views_billing.discount_preview,   name="discount_preview"),
    path("admin/plans/",                views_billing.payment_plans,      name="payment_plans"),
    path("admin/plans/remind/",         views_billing.send_plan_reminders, name="send_plan_reminders"),
    path("admin/debtors/",              views_billing.debtors,            name="debtors"),
    path("admin/debtors/<int:student_id>/", views_billing.debtor_history, name="debtor_history"),

    # Money going out
    path("admin/expenses/",             views_expenses.expenses,          name="expenses"),
    path("admin/vendors/",              views_expenses.vendors,           name="vendors"),
    path("admin/petty-cash/",           views_expenses.petty_cash,        name="petty_cash"),
    path("admin/budgets/",              views_expenses.budgets,           name="budgets"),
    path("admin/statement/",            views_expenses.income_statement,  name="income_statement"),

    # Late payment penalties and refunds
    path("admin/late-fees/",            views_recovery.late_fee_rules,    name="late_fees"),
    path("admin/refunds/",              views_recovery.refunds,           name="refunds"),

    # Bank reconciliation
    path("admin/bank/",                 views_bank.bank_reconciliation,   name="bank_reconciliation"),

    # Shop and canteen till
    path("admin/till/",                 views_shop.till,                  name="till"),
    path("admin/till/students/",        views_shop.till_students,         name="till_students"),
    path("admin/sales/",                views_shop.sales,                 name="sales"),
    path("admin/sales/<int:sale_id>/",  views_shop.sale_receipt,          name="sale_receipt"),
    path("wallet/limits/",              views_shop.set_spending_limit,    name="set_spending_limit"),

    # Withholding tax and VAT
    path("admin/tax/",                  views_tax.tax_centre,             name="tax"),
    path("admin/tax/schedule.csv",      views_tax.wht_schedule_csv,       name="wht_schedule_csv"),

    # Accounting export
    path("admin/books/",                views_export.accounting_export,   name="accounting_export"),
    path("admin/books/journal.csv",     views_export.export_journal_csv,  name="export_journal_csv"),
    path("admin/books/books.xlsx",      views_export.export_workbook,     name="export_workbook"),

    # Public receipt check (QR on every receipt points here)
    path("verify/<str:reference>/",     views_billing.verify_receipt,     name="verify_receipt"),
    path("admin/pay-on-behalf/",  views.admin_pay_on_behalf,  name="admin_pay_on_behalf"),
    # Boarder management from wallet page
    path("admin/wallet/<int:student_id>/make-boarder/",   views.make_boarder,   name="make_boarder"),
    path("admin/wallet/<int:student_id>/remove-boarder/", views.remove_boarder, name="remove_boarder"),
    path("admin/wallet/<int:student_id>/pay-hostel/",     views.pay_hostel_fee, name="pay_hostel_fee"),
]