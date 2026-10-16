**`explore map` data-quality notes no longer include cell values.** A
boolean-shaped flag note listed each encoding and its count, and an epoch
note appended a date range translated from the column min and max. `explore
map` copies those notes, so the values showed up in the map. The notes now
keep the finding and the count or fraction. The encodings stay on the column,
where `explore profile` returns them.
