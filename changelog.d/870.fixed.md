**`transform plan` no longer prints an empty relation name for models built from table functions or VALUES.**
Drafting treats `range()` / `read_csv_auto()` / VALUES as generated SQL (not warehouse relations), so the plan warning says the model is generated in SQL instead of suggesting `explore profile` on a blank name.
