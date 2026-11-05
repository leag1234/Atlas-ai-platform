Payroll reports the wrong number of working days. `count_business_days(date(2026, 11, 6), date(2026, 11, 10))`
returns 4, but the range contains only Friday 6 and Monday 9, so the answer is 2. Holidays that fall on a
weekend are also subtracted a second time.

Fix `payroll/calendar.py` so that `count_business_days` and `add_business_days` follow their docstrings for
all inputs.
