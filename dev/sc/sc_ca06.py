# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Stable CA-06 entry point for the disposable model uninstall check."""

from sc_ca06_model_unlink_purges_attachments import check
from sc_ca_runtime import run


if __name__ == '__main__':
    raise SystemExit(run('T-CA-06', check))
