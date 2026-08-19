import re
from dataclasses import dataclass, field


from recovery_service.core.enums import CorrectionActionType
from recovery_service.settings import get_settings


@dataclass
class CorrectionAction:
    type: CorrectionActionType
    priority: int = 100
    params: dict = field(default_factory=dict)


@dataclass
class OraRule:
    code: str
    patterns: list[str]
    actions: list[CorrectionAction]
    max_retries: int = 3
    _compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._compiled = [re.compile(p, re.I) for p in self.patterns]

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self._compiled)


@dataclass
class OraMatch:
    rule: OraRule
    action: CorrectionAction


class OraDictionary:
    def __init__(self, config: dict | None = None):
        if config is None:
            settings = get_settings()
            config = settings.load_yaml("ora_dictionary.yaml")
        self.rules: list[OraRule] = []
        self.default_action = CorrectionAction(type=CorrectionActionType.LOG_AND_FAIL)
        self._load(config)

    def _load(self, config: dict) -> None:
        for item in config.get("rules", []):
            actions = [
                CorrectionAction(
                    type=CorrectionActionType(a["type"]),
                    priority=a.get("priority", 100),
                    params=a.get("params", {}),
                )
                for a in item.get("actions", [])
            ]
            self.rules.append(
                OraRule(
                    code=item.get("code", "UNKNOWN"),
                    patterns=item.get("patterns", []),
                    actions=sorted(actions, key=lambda x: x.priority),
                    max_retries=item.get("max_retries", 3),
                )
            )
        default = config.get("default_action", {})
        if default:
            self.default_action = CorrectionAction(
                type=CorrectionActionType(default.get("type", "log_and_fail")),
                priority=default.get("priority", 100),
                params=default.get("params", {}),
            )

    def match(self, stderr: str) -> OraMatch | None:
        for rule in self.rules:
            if rule.matches(stderr):
                if rule.actions:
                    return OraMatch(rule=rule, action=rule.actions[0])
        return OraMatch(
            rule=OraRule(code="DEFAULT", patterns=[], actions=[self.default_action], max_retries=0),
            action=self.default_action,
        )

    def extract_ora_codes(self, text: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"ORA-\d{5}", text, re.I)))
