**A versioned `ref()` names the model.** dex used to report
`ref('dim_orders', v=1)` and `ref('dim_orders', version=1)` as unresolved.
It treated the version keyword as an unread argument and used that value as
the model name. Keyword arguments are now omitted from the positional
arguments, so the model name is the reference.
