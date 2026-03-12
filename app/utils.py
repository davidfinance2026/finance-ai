from decimal import Decimal


def money_to_decimal(value):
    if value is None:
        return Decimal("0")

    if isinstance(value, (int, float)):
        return Decimal(str(value))

    value = value.replace(".", "").replace(",", ".")
    return Decimal(value)
