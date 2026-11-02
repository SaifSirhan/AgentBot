---
name: nested-json-to-csv-aggregation
description: Aggregate nested JSON orders by customer and write a sorted CSV summary with computed totals.
---

# Nested JSON to CSV Aggregation

## Goal
Read a JSON file containing an `orders` list (each order has a `customer` and an `items` list of `{sku, qty, price}`), aggregate per customer, and write a CSV summary sorted by customer name.

## Steps

1. **Read the source JSON** from `/workspace/orders.json` without modifying it.

2. **Aggregate per customer** using a dictionary keyed by customer name:
   - `total_items` = sum of `qty` across all items in all orders for that customer.
   - `total_value` = sum of `qty * price` across all items for that customer.

3. **Sort** the aggregated rows by customer name (ascending).

4. **Write `/workspace/orders.csv`** with:
   - Header: `customer,total_items,total_value`
   - One row per customer.
   - `total_value` formatted with exactly two decimals (e.g., `f"{value:.2f}"`).

## Reference implementation

```python
import json, csv

with open("/workspace/orders.json") as f:
    data = json.load(f)

agg = {}
for order in data["orders"]:
    cust = order["customer"]
    if cust not in agg:
        agg[cust] = {"total_items": 0, "total_value": 0.0}
    for item in order["items"]:
        agg[cust]["total_items"] += item["qty"]
        agg[cust]["total_value"] += item["qty"] * item["price"]

with open("/workspace/orders.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["customer", "total_items", "total_value"])
    for cust in sorted(agg):
        writer.writerow([cust, agg[cust]["total_items"], f"{agg[cust]['total_value']:.2f}"])
```

## Notes
- Do not modify `orders.json`.
- Ensure the CSV has no extra whitespace and uses `\n` line endings (default `csv.writer` behavior).
- Verify the output matches the expected header and sorted rows.
