"""Stable public errors: never include upstream bodies, URLs or credentials."""


class HeadscaleError(ValueError):
    def __init__(self, code: str, *, http_status=None, grpc_code=None, outcome="not_sent",
                 category=None, diagnostic=None, policy_verification=None):
        self.code = code
        self.http_status = http_status
        self.grpc_code = grpc_code
        self.outcome = outcome
        # Only adapter-owned categories/hints and digests are supplied here;
        # never pass raw exception messages, upstream bodies or policy text.
        self.category = category
        self.diagnostic = diagnostic
        self.policy_verification = policy_verification
        super().__init__(code + ("; outcome unknown; read back before retrying" if outcome == "unknown" else ""))

    def as_dict(self):
        result = {"code": self.code, "http_status": self.http_status,
                  "grpc_code": self.grpc_code, "outcome": self.outcome, "retryable": False}
        for field in ("category", "diagnostic", "policy_verification"):
            value = getattr(self, field)
            if value is not None:
                result[field] = value
        return result

