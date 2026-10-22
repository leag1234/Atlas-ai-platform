#!/usr/bin/env python3
"""Print today's private daily-budget totals without conversation content."""
import json
from services.orchestrator.daily_budget import DailyLedger

rows = DailyLedger().summary()
print(json.dumps({"users": rows, "global": {"measured_eur": sum(float(r["measured_eur"]) for r in rows), "searches": sum(int(r["searches"]) for r in rows)}}, sort_keys=True))
