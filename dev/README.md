# Development helpers

The Odoo integration tests run in an isolated sandbox through the project
launcher. `coverage.sh` produces the release coverage result.

`SLOT=<slot> WOOW_SBX=<absolute launcher> WOOW_SBX_BASE_DB=<existing sandbox DB>
sh dev/sc/run_sc.sh share-max-downloads-race` runs T-SHARE-32 through two real
HTTP clients in a copied sandbox database. The runner checks that the selected
test actually ran and drops its database. The other `sc_*.py` entries and
`sandbox_test.sh` exit with an error until they are implemented. A scenario
result must not be reported as passing unless its runner was executed.

The real HTTP runners below install the addon into a copied database, start a
temporary Odoo server bound to container loopback (every request then runs in
its own Repeatable Read transaction, unlike HttpCase), check the persisted
counters with psql, stop the server and drop the database. Denied counters are
kept per minute of the database clock, so each burst starts only when enough
of the current minute remains, and a burst that still crossed a minute is
repeated once on clean counters.

- `sh dev/sc/run_sc.sh denied-count-real-http` runs T-SHARE-44: 11 refused
  link opens from one source answer 10 x 404 and 1 x 429; the source bucket
  counts 10, one `rate_limited` event is recorded, and the source counter is 10.
- `sh dev/sc/run_sc.sh source-limit-two-links` runs T-SHARE-45: two clients
  from one source send wrong passwords to two protected links in rounds that
  start together. With a limit of 3 across links, exactly 3 guesses are
  compared; the others answer 429, and the source counter and failure buckets
  both count 3. The correct password sent afterwards from that source is
  answered 429 as well.
- `sh dev/sc/run_sc.sh denied-sources-together` runs T-SHARE-46: twelve
  clients, each its own source through `X-Real-IP` behind the trusted loopback
  proxy, send refused link opens in rounds that start together, and a correct
  password is sent to a protected link in the first round. No request may be
  retried for a concurrency error (the server log is checked), every refused
  open answers 404, the unlock answers 303, and the capacity counts exactly
  one slot per new counter row and per new bucket.

- `sh dev/sc/run_sc.sh denied-many-sources` runs T-SHARE-47: sixty clients, each its own
  source, send refused link requests in rounds that start together to a threaded server
  (`--workers=0`, no `ODOO_MAX_HTTP_THREADS`, `db_maxconn` 64). Every request holds one
  connection and every new source borrows a second one for its capacity slots, so an
  unlimited burst would take connections that other requests need and the pool would run
  dry. Every answer must be 404 or 429 and none a server error, the server log must show
  no error trace and no retry, and the counters must equal the refusals that were answered
  404 (the reservations that find no turn or no connection are answered 429).

All four use the same `SLOT`, `WOOW_SBX` and `WOOW_SBX_BASE_DB` variables as above.
