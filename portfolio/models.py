from django.db import models


class Kind(models.TextChoices):
    """What a timeline event means for the account. See broker/mapping.py."""

    DEPOSIT = "deposit", "Deposit"
    WITHDRAWAL = "withdrawal", "Withdrawal"  # back to the user's own bank
    CARD = "card", "Card spending"  # lifestyle consumption
    TAX_REFUND = "tax_refund", "Tax refund"
    BUY = "buy", "Buy"
    SELL = "sell", "Sell"
    DIVIDEND = "dividend", "Dividend"
    INTEREST = "interest", "Interest"
    BOND_REDEMPTION = "bond_redemption", "Bond redemption"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"
    REJECTED = "rejected", "Rejected"
    PENDING = "pending", "Pending"
    UNKNOWN = "unknown", "Other"


CASH_KINDS = {
    Kind.DEPOSIT, Kind.WITHDRAWAL, Kind.CARD, Kind.TAX_REFUND, Kind.BUY, Kind.SELL,
    Kind.DIVIDEND, Kind.INTEREST, Kind.BOND_REDEMPTION,
}
INCOME_KINDS = {Kind.DIVIDEND, Kind.INTEREST}


class Instrument(models.Model):
    class Category(models.TextChoices):
        STOCKS_ETFS = "stocksAndETFs", "Stocks & ETFs"
        CRYPTOS = "cryptos", "Crypto"
        BONDS = "bonds", "Bonds"
        PRIVATE_MARKETS = "privateMarkets", "Private markets"
        OTHER = "others", "Other"

    isin = models.CharField(max_length=12, primary_key=True)
    name = models.CharField(max_length=200, blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    is_etf = models.BooleanField(default=False)
    exchange = models.CharField(max_length=20, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name or self.isin

    @property
    def country(self) -> str:
        """ISIN prefix = country of domicile (XS = international, crypto = XF...)."""
        return self.isin[:2]

    @property
    def asset_class(self) -> str:
        if self.category == self.Category.CRYPTOS:
            return "Crypto"
        if self.category == self.Category.BONDS:
            return "Bonds"
        if self.category == self.Category.PRIVATE_MARKETS:
            return "Private markets"
        return "ETFs" if self.is_etf else "Stocks"


class Transaction(models.Model):
    """One Trade Republic timeline event (timelineTransactions or timelineActivityLog)."""

    tr_id = models.CharField(max_length=80, unique=True)
    source = models.CharField(max_length=16)  # "transactions" | "activity"
    timestamp = models.DateTimeField(db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    amount = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")
    instrument = models.ForeignKey(
        Instrument, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions"
    )
    title = models.CharField(max_length=300, blank=True)
    subtitle = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=40, blank=True)
    event_type = models.CharField(max_length=80, blank=True, db_index=True)
    event_subtype = models.CharField(max_length=200, blank=True)
    # Filled by timelineDetailV2 enrichment (optional).
    shares = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    price = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    fees = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    taxes = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    raw = models.JSONField(default=dict)
    detail = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["kind", "timestamp"])]

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d} {self.kind} {self.amount} {self.title}"

    @property
    def label(self) -> str:
        """Most informative text: bond events carry the meaning in the subtitle."""
        generic_title = not self.title or self.title[-4:].isdigit()  # e.g. "Feb 2026"
        if self.kind in (Kind.INTEREST, Kind.BOND_REDEMPTION, Kind.DIVIDEND) and self.subtitle \
                and generic_title:
            return f"{self.subtitle} — {self.title}" if self.title else self.subtitle
        return self.title or self.subtitle or self.get_kind_display()


class PortfolioSnapshot(models.Model):
    """State of the account at the end of a sync — builds real net-worth history."""

    taken_at = models.DateTimeField(db_index=True)
    sync_run = models.ForeignKey(
        "broker.SyncRun", null=True, blank=True, on_delete=models.SET_NULL
    )
    cash = models.DecimalField(max_digits=18, decimal_places=2)
    depot_value = models.DecimalField(max_digits=18, decimal_places=2)
    depot_cost = models.DecimalField(max_digits=18, decimal_places=2)
    raw = models.JSONField(default=dict)

    class Meta:
        ordering = ["-taken_at"]
        get_latest_by = "taken_at"

    @property
    def total(self):
        return self.cash + self.depot_value


class PositionSnapshot(models.Model):
    snapshot = models.ForeignKey(
        PortfolioSnapshot, on_delete=models.CASCADE, related_name="positions"
    )
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="snapshots")
    quantity = models.DecimalField(max_digits=24, decimal_places=8)
    avg_cost = models.DecimalField(max_digits=20, decimal_places=6)
    price = models.DecimalField(max_digits=20, decimal_places=6, null=True)
    value = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        ordering = ["-value"]
        constraints = [
            models.UniqueConstraint(fields=["snapshot", "instrument"], name="uniq_position")
        ]

    @property
    def cost(self):
        return self.quantity * self.avg_cost

    @property
    def pl(self):
        return self.value - self.cost

    @property
    def pl_pct(self):
        return (self.pl / self.cost * 100) if self.cost else None


class BenchmarkPrice(models.Model):
    symbol = models.CharField(max_length=20)
    date = models.DateField()
    close = models.DecimalField(max_digits=18, decimal_places=4)

    class Meta:
        ordering = ["symbol", "date"]
        constraints = [models.UniqueConstraint(fields=["symbol", "date"], name="uniq_bench_day")]


class Document(models.Model):
    path = models.CharField(max_length=500, unique=True)  # relative to DOCUMENTS_DIR
    title = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=40, blank=True)
    date = models.DateField(null=True, blank=True)
    isin = models.CharField(max_length=12, blank=True)
    tr_event_id = models.CharField(max_length=80, blank=True, db_index=True)

    class Meta:
        ordering = ["-date"]
