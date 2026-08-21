from __future__ import annotations

import ipaddress


def ip_allowed(value: str, rules_text: str) -> bool:
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return False
    rules = [item.strip() for item in (rules_text or "").split(",") if item.strip()]
    if not rules:
        return address.is_loopback
    for rule in rules:
        if rule == "*":
            return True
        try:
            if "-" in rule:
                start_text, end_text = [item.strip() for item in rule.split("-", 1)]
                start, end = ipaddress.ip_address(start_text), ipaddress.ip_address(end_text)
                if start.version != end.version or int(start) > int(end):
                    continue
                if address.version == start.version and int(start) <= int(address) <= int(end):
                    return True
            elif "/" in rule:
                if address in ipaddress.ip_network(rule, strict=False):
                    return True
            elif address == ipaddress.ip_address(rule):
                return True
        except ValueError:
            continue
    return False
