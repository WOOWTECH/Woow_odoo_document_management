# Development helpers

The Odoo integration tests run in an isolated sandbox through the project
launcher. `coverage.sh` produces the release coverage result.

`SLOT=<slot> WOOW_SBX=<absolute launcher> WOOW_SBX_BASE_DB=<existing sandbox DB>
sh dev/sc/run_sc.sh share-max-downloads-race` runs T-SHARE-32 through two real
HTTP clients in a copied sandbox database. The runner checks that the selected
test actually ran and drops its database. The other `sc_*.py` entries and
`sandbox_test.sh` exit with an error until they are implemented. A scenario
result must not be reported as passing unless its runner was executed.

`SLOT=<slot> WOOW_SBX=<absolute launcher> WOOW_SBX_BASE_DB=<existing sandbox DB>
sh dev/sc/run_sc.sh denied-count-real-http` runs T-SHARE-44 against a temporary
Odoo server bound to container loopback. It uses curl for 11 denied POSTs and
psql to check the persisted counter. The runner stops the server and drops its
copied database.
