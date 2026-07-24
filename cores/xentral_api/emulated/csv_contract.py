from __future__ import annotations

from typing import Any


def field(
    type_: str = "string",
    *,
    section: str = "general",
    access: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": type_, "section": section}
    if access:
        spec["access"] = access
    spec.update(extra)
    return spec


MONEY_READ = field("decimal", section="financials", access="readOnly")
MONEY_WRITE = field("decimal", section="financials")

DOCUMENT_COMMON_ROOT = {
    "paymentMethod": field(section="financials"),
    "paymentMethodName": field(section="financials"),
    "paymentStatus": field("select", section="financials"),
    "paymentTargetDays": field("integer", section="financials"),
    "paymentTargetDiscountDays": field("integer", section="financials"),
    "paymentTargetDiscount": MONEY_WRITE,
    "currency": field(section="financials"),
    "taxation": field("select", section="financials"),
    "vatId": field(section="address"),
    "email": field(section="address"),
    "phone": field(section="address"),
    "fax": field(section="address"),
    "sentAt": field("datetime", access="readOnly"),
    "sentVia": field("select", access="readOnly"),
    "sentBy": field(access="readOnly"),
    "customerOrderNumber": field(section="references"),
    "deliveryDate": field("date", section="shipping"),
    "discount": MONEY_WRITE,
    "discountTiers": MONEY_WRITE,
    "totalGrossAmount": MONEY_READ,
    "grossAmount": MONEY_READ,
    "totalGross": MONEY_READ,
    "netAmount": MONEY_READ,
    "netRevenue": MONEY_READ,
    "contributionMargin": MONEY_READ,
    "isContributionMarginCalculated": field("boolean", access="readOnly"),
    "pdfArchiveCount": field("integer", access="readOnly"),
    "pdfArchiveVersion": field("integer", access="readOnly"),
    "pdfArchivedVersion": field("integer", access="readOnly"),
    "isPdfArchived": field("boolean", access="readOnly"),
    "shouldArchive": field("boolean"),
    "shouldBeArchived": field("boolean"),
    "deliveryAddressId": field(
        "reference", section="address", reference="DeliveryAddress", renderProperty="name"
    ),
}

LINE_ITEM_COMMON = {
    "productId": field("reference", reference="Product", renderProperty="name"),
    "projectId": field("reference", reference="Project", renderProperty="name"),
    "netPrice": MONEY_WRITE,
    "currency": field(section="financials"),
    "status": field("select"),
    "salesTaxType": field("select", section="financials"),
    "costCenter": field(section="financials"),
    "costCenterValue": field(section="financials"),
    "revenueAccountValue": field(section="financials"),
    "purchasePriceCurrency": field(section="financials"),
    "originalPurchasePrice": MONEY_WRITE,
    "purchasePriceId": field(
        "reference", section="financials", reference="PurchasePrice", renderProperty="id"
    ),
    "isRevenueAccountLocked": field("boolean", section="financials"),
    "isRevenueFixed": field("boolean", section="financials"),
    "shouldPrintWithoutPrice": field("boolean", section="content"),
    "isHiddenOnPdf": field("boolean", section="content"),
    "shouldHideOnPdf": field("boolean", section="content"),
    "cashDiscountAmount": MONEY_READ,
    "taxAmount": MONEY_READ,
    "netRevenueItemSingle": MONEY_READ,
    "netRevenueItemTotal": MONEY_READ,
    "grossRevenueItemSingle": MONEY_READ,
    "grossRevenueItemTotal": MONEY_READ,
    "netRevenueSingle": MONEY_READ,
    "netRevenueTotal": MONEY_READ,
    "grossRevenueSingle": MONEY_READ,
    "grossRevenueTotal": MONEY_READ,
    "xglNetAmount": MONEY_READ,
    "xglTaxAmount": MONEY_READ,
    "xglGrossAmount": MONEY_READ,
    "xglTaxRate": field("decimal", section="financials", access="readOnly"),
    "xglRoundedTaxRate": field("decimal", section="financials", access="readOnly"),
}
