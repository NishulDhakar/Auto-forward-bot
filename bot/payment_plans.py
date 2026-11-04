"""Shared subscription pricing; keep migration_reliability.sql validation in sync."""
PLANS = {
    "1_month": {
        "label": "1 Month",
        "inr": 88,
        "usdt": 2,
        "days": 30,
        "per_day_inr": "₹2.9/day",
    },
    "3_months": {
        "label": "3 Months",
        "inr": 250,
        "usdt": 5,
        "days": 90,
        "per_day_inr": "₹2.7/day",
    },
    "6_months": {
        "label": "6 Months",
        "inr": 500,
        "usdt": 10,
        "days": 180,
        "per_day_inr": "₹2.7/day",
    },
}
