---
name: csv-summary
description: Read a CSV with product, quantity, and unit_price columns and write a JSON summary with row count, total quantity, and total revenue.
---

# CSV Summary

## Steps

1. Read `/workspace/sales.csv` and confirm it has columns `product`, `quantity`, `unit_price`.
2. Parse the data rows (skip the header).
3. Compute:
   - `rows`: number of data rows
   - `total_quantity`: sum of `quantity`
   - `total_revenue`: sum of `quantity * unit_price`, rounded to 2 decimals
4. Write `/workspace/summary.json` with exactly the keys `rows`, `total_quantity`, `total_revenue`.
5. Do not modify `sales.csv`.

## Example

```python
import csv, json

rows = 0
total_quantity = 0
total_revenue = 0.0

with open("/workspace/sales.csv", newline="") as f:
    reader = csv.DictReader(f)
    for r in reader:
        q = int(r["quantity"])
        p = float(r["unit_price"])
        rows += 1
        total_quantity += q
        total_revenue += q * p

summary = {
    "rows": rows,
    "total_quantity": total_quantity,
    "total_revenue": round(total_revenue, 2),
}

with open("/workspace/summary.json", "w") as f:
    json.dump(summary, f)
```

## Notes

- Round only the final `total_revenue` value to two decimals.
- Ensure the output JSON contains exactly the three required keys.
