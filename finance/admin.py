from django.contrib import admin
from .models import (
    Wallet, Transaction, FeeStructure, SchoolItem,
    Invoice, InvoiceItem, Payment,
    ParentBilling, ChildBilling, ChildPaymentAllocation,
    Installment, FeePayment, Purchase, TopUpRequest, AuditLog,
)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display  = ['student', 'balance', 'tenant']
    search_fields = ['student__full_name']
    list_filter   = ['tenant']


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display  = ['reference', 'wallet', 'txn_type', 'category', 'amount', 'status', 'created_at']
    list_filter   = ['txn_type', 'category', 'status', 'tenant']
    search_fields = ['reference', 'wallet__student__full_name']


@admin.register(FeeStructure)
class FeeStructureAdmin(admin.ModelAdmin):
    list_display  = ['name', 'amount', 'is_active', 'tenant']
    list_filter   = ['is_active', 'tenant']
    search_fields = ['name']


@admin.register(SchoolItem)
class SchoolItemAdmin(admin.ModelAdmin):
    list_display  = ['name', 'tenant']
    search_fields = ['name']
    list_filter   = ['tenant']


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display  = ['invoice_no', 'student', 'total', 'amount_paid', 'balance_due', 'status', 'created_at']
    list_filter   = ['status', 'tenant']
    search_fields = ['invoice_no', 'student__full_name']
    inlines       = [InvoiceItemInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display  = ['reference', 'wallet', 'amount', 'method', 'status', 'created_at']
    list_filter   = ['status', 'method', 'tenant']
    search_fields = ['reference', 'wallet__student__full_name']


@admin.register(ParentBilling)
class ParentBillingAdmin(admin.ModelAdmin):
    list_display  = ['wallet', 'session', 'term', 'total_expected', 'total_paid', 'balance']
    list_filter   = ['session', 'term', 'tenant']
    search_fields = ['wallet__student__full_name']


@admin.register(ChildBilling)
class ChildBillingAdmin(admin.ModelAdmin):
    list_display  = ['student', 'parent_billing', 'total_fee', 'paid', 'balance']
    list_filter   = ['tenant']
    search_fields = ['student__full_name']


@admin.register(ChildPaymentAllocation)
class ChildPaymentAllocationAdmin(admin.ModelAdmin):
    list_display  = ['payment', 'child_billing', 'amount_allocated', 'created_at']
    list_filter   = ['tenant']


@admin.register(Installment)
class InstallmentAdmin(admin.ModelAdmin):
    list_display  = ['wallet', 'payment', 'invoice', 'amount', 'paid_at']
    list_filter   = ['tenant']


@admin.register(FeePayment)
class FeePaymentAdmin(admin.ModelAdmin):
    list_display  = ['student', 'fee_structure', 'amount_paid', 'balance_due', 'status', 'paid_at']
    list_filter   = ['status', 'tenant']
    search_fields = ['student__full_name']


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display  = ['student', 'item', 'quantity', 'total_price', 'status', 'created_at']
    list_filter   = ['status', 'tenant']
    search_fields = ['student__full_name']


@admin.register(TopUpRequest)
class TopUpRequestAdmin(admin.ModelAdmin):
    list_display  = ['wallet', 'amount', 'method', 'status', 'requested_at']
    list_filter   = ['status', 'method', 'tenant']
    search_fields = ['wallet__student__full_name']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display  = ['action', 'performed_by', 'target_model', 'target_id', 'created_at']
    list_filter   = ['action', 'tenant']
    search_fields = ['description', 'target_model']