"""Payout Engine — URL routing."""

from django.urls import path

from .views import (
    BalanceView,
    CreditMerchantView,
    LedgerView,
    MerchantDetailView,
    MerchantListCreateView,
    PayoutCreateView,
    PayoutDetailView,
    PayoutListView,
)

urlpatterns = [
    # Merchant endpoints
    path("merchants/", MerchantListCreateView.as_view(), name="merchant-list-create"),
    path("merchants/<uuid:merchant_id>/", MerchantDetailView.as_view(), name="merchant-detail"),
    path("merchants/<uuid:merchant_id>/balance/", BalanceView.as_view(), name="merchant-balance"),
    path("merchants/<uuid:merchant_id>/ledger/", LedgerView.as_view(), name="merchant-ledger"),
    path("merchants/<uuid:merchant_id>/credit/", CreditMerchantView.as_view(), name="merchant-credit"),

    # Payout endpoints
    path("payouts/", PayoutCreateView.as_view(), name="payout-create"),
    path("payouts/list/", PayoutListView.as_view(), name="payout-list"),
    path("payouts/<uuid:payout_id>/", PayoutDetailView.as_view(), name="payout-detail"),
]
