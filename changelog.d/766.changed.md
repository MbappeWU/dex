**`semantic plan` warns when a cumulative metric omits `period_agg`.**
MetricFlow fills `first` for an omitted `cumulative_type_params.period_agg`.
At week or month grain the metric reports that period's first day, and the
plan used to store the metric with no warning. `semantic define` and
`semantic update` warn the same way. Set `period_agg: last` for a running
total.
