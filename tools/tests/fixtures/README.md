# Static tool fixtures

Files end in `.fixture` so that linters and scanners never treat them as code; the tests copy
them into a temporary repository and strip the suffix. A first line `#! target=<path> expect=<RULE>`
names the destination path and the rule the snippet must trigger. Every fixture is synthetic.
