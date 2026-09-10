"""Public projections never expose email-routing capabilities."""

import copy
import re

from unblock.domain import awaiting_reply

CAPABILITY = re.compile(r"case-[0-9a-f]{32}(?:\+[^\s@]*)?@[^\s<>\"']+", re.IGNORECASE)


def sample_case(case):
    return case.get("tenant") == "demo" or case.get("read_only") is True


def redact(value, token=""):
    if isinstance(value, dict):
        return {
            k: redact(v, token)
            for k, v in value.items()
            if k not in {"reply_token", "reply_to", "reply_address"}
        }
    if isinstance(value, list):
        return [redact(v, token) for v in value]
    if isinstance(value, str):
        value = CAPABILITY.sub("[private reply address]", value)
        return value.replace(token, "[private reply token]") if token else value
    return value


def present(case):
    result = redact(copy.deepcopy(case), case.get("reply_token") or "")
    result["awaiting_reply"] = awaiting_reply(case)
    result["sample"] = sample_case(case)
    if result["sample"]:
        result["provenance"] = (
            "Simulated sample walkthrough; these events are not a recorded live run."
        )
        for request in result.get("requests", []):
            request["delivery_mode"] = "simulated"
    return result
