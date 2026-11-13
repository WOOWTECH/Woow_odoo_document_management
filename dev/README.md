# Development helpers

The Odoo integration tests run in an isolated sandbox through the project
launcher. `coverage.sh` produces the release coverage result. `sandbox_test.sh`
runs the whole addon suite (the G1 gate: the floors of
`woow_document_management/tests/EXPECTED_MIN.json`, no skipped test, and the
success line of the browser tour); only the runner listed here is registered in
`run_sc.sh`. A scenario result must not be reported as passing unless its runner
was executed.

The real HTTP runner installs the addon into a copied database, starts a
temporary Odoo server bound to container loopback (every request then runs in
its own Repeatable Read transaction, unlike HttpCase), checks the result, stops
the server and drops the database.

- `SLOT=<slot> WOOW_SBX=<absolute launcher> WOOW_SBX_BASE_DB=<existing sandbox DB>
  sh dev/sc/run_sc.sh quota-contention` runs T-UPL-32: with a quota set and a
  long attachment table, one portal account sends single-file uploads without
  pause while another one uploads once a second. Every upload answers 200, the
  slower account's within a second at the 95th percentile, and no request is
  rolled back and run again for a concurrency error.
