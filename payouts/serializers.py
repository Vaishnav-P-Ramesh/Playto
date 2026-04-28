"""
Payout Engine — Serializers
============================
"""

from rest_framework import serializers

from .models import LedgerEntry, Merchant, Payout


class MerchantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = ["id", "name", "created_at"]


class PayoutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payout
        fields = [
            "id",
            "merchant",
            "amount_paise",
            "bank_account_id",
            "status",
            "retries_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "retries_count", "created_at", "updated_at"]


class CreatePayoutSerializer(serializers.Serializer):
    """
    Validates the POST /api/v1/payouts request body.

    We use a plain Serializer (not ModelSerializer) so we can enforce
    business rules before touching the DB.
    """

    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.CharField(max_length=255)

    def validate_amount_paise(self, value: int) -> int:
        # Minimum payout: ₹1 (100 paise). Prevents micro-payout spam.
        if value < 100:
            raise serializers.ValidationError(
                "amount_paise must be at least 100 (₹1)."
            )
        return value


class LedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerEntry
        fields = [
            "id",
            "merchant",
            "amount",
            "type",
            "reference_payout",
            "created_at",
        ]


class BalanceSerializer(serializers.Serializer):
    """
    Response serializer for the balance endpoint.

    available_balance  = net SUM of all ledger entries (can include held amounts)
    held_balance       = absolute sum of HOLD-type entries (funds reserved)
    withdrawable       = available_balance - held_balance
    """

    available_balance = serializers.IntegerField()
    held_balance = serializers.IntegerField()
    withdrawable = serializers.IntegerField()
    merchant_id = serializers.UUIDField()
    merchant_name = serializers.CharField()
