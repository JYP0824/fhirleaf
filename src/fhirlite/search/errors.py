"""Search errors and their OperationOutcome."""
from __future__ import annotations


class SearchError(Exception):
    def __init__(self, status, diagnostics, code="processing"):
        super().__init__(diagnostics)
        self.status, self.diagnostics, self.code = status, diagnostics, code


def operation_outcome(error):
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": error.code, "diagnostics": error.diagnostics}]}
