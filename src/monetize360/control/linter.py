"""Static checks: schema, unknown signals (CEL type-check), floor>ceiling, above tenant max, banned signals,
unreachable/overlapping rules, missing defaults. Warn with the highest base_price the pack can price when
floor is pct_of_base and ceiling is abs (hotel: 5000 / 0.70 = 7142.86) - above it every request is a
bounds_conflict (engine review #4)."""
