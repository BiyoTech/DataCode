"""Rule Engine — 规则加载、解析和执行。

从 .agent/rules/ 目录加载 Markdown 规则文件，
解析 YAML frontmatter 元数据，并注入到 Agent 系统提示词中。

规则类型:
- Prompt Rule: 注入到系统提示词
- Guardrail Rule: 执行前验证
- Tool Rule: 工具访问控制
- Output Rule: 执行后验证
"""

import os
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Rule:
    """解析后的规则对象。"""
    name: str
    priority: int = 10
    scope: str = "workspace"  # workspace | project | global
    categories: t.List[str] = field(default_factory=list)
    agents: t.List[str] = field(default_factory=list)  # 空 = 所有 Agent
    forbid: t.List[str] = field(default_factory=list)
    require: t.List[str] = field(default_factory=list)
    content: str = ""  # Markdown 正文 (注入到系统提示词)
    source_file: str = ""


class RuleEngine:
    """规则引擎 — 管理规则的加载、过滤和执行。"""

    def __init__(self):
        self._rules: t.List[Rule] = []

    def load_rules_from_dir(self, rules_dir: str) -> t.List[Rule]:
        """从目录加载所有 .md 规则文件。

        按文件名排序 (数字前缀控制优先级)。
        """
        self._rules = []

        if not os.path.exists(rules_dir):
            return self._rules

        rule_files = sorted(
            [f for f in os.listdir(rules_dir) if f.endswith(".md")]
        )

        for filename in rule_files:
            filepath = os.path.join(rules_dir, filename)
            rule = self._parse_rule_file(filepath)
            if rule:
                self._rules.append(rule)
                logger.info("rule_loaded", name=rule.name, file=filename)

        return self._rules

    def get_rules_for_agent(self, agent_name: str) -> t.List[Rule]:
        """获取适用于指定 Agent 的规则。

        如果规则的 agents 列表为空，则适用于所有 Agent。
        """
        return [
            rule for rule in self._rules
            if not rule.agents or agent_name in rule.agents
        ]

    def get_prompt_injection(self, agent_name: str) -> str:
        """获取需要注入到 Agent 系统提示词的规则内容。"""
        rules = self.get_rules_for_agent(agent_name)
        if not rules:
            return ""

        sections = []
        for rule in sorted(rules, key=lambda r: r.priority):
            if rule.content:
                sections.append(f"## Rule: {rule.name}\n\n{rule.content}")

        return "\n\n---\n\n".join(sections)

    def check_guardrails(self, agent_name: str, text: str) -> t.List[str]:
        """检查文本是否违反 guardrail 规则。

        Returns:
            违规消息列表 (空 = 无违规)。
        """
        violations = []
        rules = self.get_rules_for_agent(agent_name)

        for rule in rules:
            for forbidden in rule.forbid:
                # 简单的关键词匹配 (MVP 阶段)
                if _matches_guardrail(forbidden, text):
                    violations.append(
                        f"Rule '{rule.name}' violation: {forbidden}"
                    )

        return violations

    def check_requirements(self, agent_name: str, text: str) -> t.List[str]:
        """检查文本是否满足 require 规则。

        Returns:
            未满足的要求列表 (空 = 全部满足)。
        """
        # MVP 阶段: require 规则仅作为提示注入，不做强制检查
        return []

    def _parse_rule_file(self, filepath: str) -> t.Optional[Rule]:
        """解析规则文件 (Markdown + YAML frontmatter)。"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning("rule_file_read_error", file=filepath, error=str(e))
            return None

        # 解析 YAML frontmatter
        frontmatter = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError as e:
                    logger.warning("rule_frontmatter_parse_error", file=filepath, error=str(e))
                body = parts[2].strip()

        return Rule(
            name=frontmatter.get("name", Path(filepath).stem),
            priority=frontmatter.get("priority", 10),
            scope=frontmatter.get("scope", "workspace"),
            categories=frontmatter.get("categories", []),
            agents=frontmatter.get("agents", []),
            forbid=frontmatter.get("forbid", []),
            require=frontmatter.get("require", []),
            content=body,
            source_file=filepath,
        )


def _matches_guardrail(pattern: str, text: str) -> bool:
    """简单的 guardrail 匹配 (MVP: 关键词检查)。"""
    # 提取关键词 (去掉 "Never use", "Do not" 等前缀)
    keywords = pattern.lower()
    for prefix in ["never use ", "never ", "do not use ", "do not ", "don't use ", "don't "]:
        if keywords.startswith(prefix):
            keywords = keywords[len(prefix):]
            break

    return keywords in text.lower()


# 全局单例
rule_engine = RuleEngine()
