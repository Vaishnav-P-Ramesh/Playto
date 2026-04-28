"""Payout Engine — Django Admin registrations."""

from django.contrib import admin

from .models import IdempotencyKey, LedgerEntry, Merchant, Payout


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "created_at")
    search_fields = ("name",)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "amount_paise", "status", "retries_count", "created_at")
    list_filter = ("status",)
    search_fields = ("merchant__name",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "amount", "type", "reference_payout", "created_at")
    list_filter = ("type",)
    readonly_fields = ("id", "created_at")


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "key", "created_at")
    readonly_fields = ("id", "created_at")
