"""Number formatting for templates. Minus is a true minus sign (−)."""


from django import template

register = template.Library()
MINUS = "−"


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@register.filter
def eur(v, digits=2):
    x = _f(v)
    if x is None:
        return "—"
    s = f"€{abs(x):,.{int(digits)}f}"
    return f"{MINUS}{s}" if x < 0 and round(abs(x), int(digits)) else s


@register.filter
def eur0(v):
    return eur(v, 0)


@register.filter
def signed_eur(v, digits=2):
    x = _f(v)
    if x is None:
        return "—"
    if round(x, int(digits)) == 0:
        return eur(0, digits)
    return ("+" if x > 0 else MINUS) + f"€{abs(x):,.{int(digits)}f}"


@register.filter
def pct(v, digits=1):
    x = _f(v)
    return "—" if x is None else f"{x:,.{int(digits)}f}%".replace("-", MINUS)


@register.filter
def signed_pct(v, digits=1):
    x = _f(v)
    if x is None:
        return "—"
    if round(x, int(digits)) == 0:
        return f"{0:.{int(digits)}f}%"
    return ("+" if x > 0 else MINUS) + f"{abs(x):,.{int(digits)}f}%"


@register.filter
def tone(v):
    x = _f(v)
    if x is None or abs(x) < 0.005:
        return ""
    return "gain" if x > 0 else "loss"


@register.filter
def qty(v, asset_class=""):
    """Whole shares without decimals; fractional up to 4; crypto up to 6."""
    x = _f(v)
    if x is None:
        return "—"
    if abs(x - round(x)) < 1e-9:
        return f"{x:,.0f}"
    places = 6 if asset_class == "Crypto" else 4
    return f"{x:,.{places}f}".rstrip("0").rstrip(".")


KIND_COLOURS = {
    "buy": "--s1", "sell": "--s2", "dividend": "--s3", "interest": "--s3",
    "bond_redemption": "--s5", "deposit": "--gain", "tax_refund": "--gain",
    "withdrawal": "--s4", "card": "--s4",
}


@register.filter
def kind_colour(kind):
    return f"var({KIND_COLOURS.get(kind, '--ink-3')})"


ASSET_COLOURS = {"Stocks": "--s1", "ETFs": "--s2", "Crypto": "--s3", "Bonds": "--s4",
                 "Private markets": "--s5", "Cash": "--s-cash"}
ASSET_LABEL_INK = {"ETFs": "dark", "Cash": "cash"}


@register.filter
def asset_colour(label):
    return f"var({ASSET_COLOURS.get(label, '--ink-3')})"


@register.filter
def asset_label_ink(label):
    return ASSET_LABEL_INK.get(label, "light")
