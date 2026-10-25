**`transform plan` drops a Dex-scaffolded empty `.gitkeep` when the first model lands.**
When a plan upserts the first real file into `models/staging`, `intermediate`, or
`marts` and that folder still holds only an empty `.gitkeep` from `transform init`,
the plan includes a delete for the keeper so apply removes it under the same
guard.
