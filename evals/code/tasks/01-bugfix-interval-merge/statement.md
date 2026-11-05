A user of the `rangeset` package reports that `merge_intervals` sometimes returns ranges that still overlap.
For example `merge_intervals([(5, 7), (1, 3), (2, 4)])` should give `[(1, 4), (5, 7)]`, and
`merge_intervals([(1, 3), (3, 5)])` should give `[(1, 5)]` because the intervals are closed and touch.

Fix `merge_intervals` so it follows its docstring for any input. Keep the public signature unchanged.
