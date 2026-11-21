ITEMS = [
    {"sku": "B-200", "name": "Bolt M6", "qty": 120},
    {"sku": "A-100", "name": "Anchor 8mm", "qty": 15},
    {"sku": "C-300", "name": "Clamp 2in", "qty": 0},
]


def load_items():
    return [dict(i) for i in ITEMS]
