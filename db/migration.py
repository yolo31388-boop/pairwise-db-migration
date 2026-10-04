"""
游戏内存档迁移与版本升级系统 - 核心模块

功能：
- detect_version: 版本检测（容错 + 向前兼容）
- migrate_schema: schema 迁移（有顺序 + 事务）
- data_migration: 数据迁移（转换规则 + 验证）
- rollback: 回滚（备份 + 版本记录）
- compatibility: 兼容性处理（默认值 + 警告）
"""
import copy
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


class MigrationError(Exception):
    """迁移过程中的可恢复错误。"""


class VersionDetectionError(MigrationError):
    """无法识别存档版本。"""


@dataclass
class Config:
    current_version: int = 3
    supported_versions: Tuple[int, ...] = (1, 2, 3)


@dataclass
class MigrationResult:
    success: bool
    from_version: Optional[int] = None
    to_version: Optional[int] = None
    data: Optional[Dict] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# 各版本存档的 schema 字段（用于版本推断与兼容性默认值）
SCHEMA_FIELDS: Dict[int, Tuple[str, ...]] = {
    1: ("player_name", "level", "hp"),
    2: ("player_name", "level", "hp", "mp", "inventory"),
    3: ("player_name", "level", "hp", "mp", "inventory", "skills", "achievements"),
}

# 兼容性处理时缺失字段的默认值
FIELD_DEFAULTS: Dict[str, object] = {
    "player_name": "Unknown",
    "level": 1,
    "hp": 100,
    "mp": 50,
    "inventory": list,
    "skills": list,
    "achievements": list,
}


def _default_for(field_name: str):
    value = FIELD_DEFAULTS.get(field_name)
    if callable(value):
        return value()
    return copy.deepcopy(value)


class DatabaseMigration:
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.current_version = int(self.config.get("current_version", 3))
        self._state = {}
        self._history: List[Dict] = []
        # 备份栈：{"version": int, "data": dict}
        self._backups: List[Dict] = []
        # schema 迁移步骤：from_version -> [{"name", "to", "fn", "requires"}]
        self._schema_steps: Dict[int, List[Dict]] = {}
        # 数据转换规则：(from_version, to_version) -> fn
        self._data_rules: Dict[Tuple[int, int], Callable[[Dict], Dict]] = {}
        # 数据验证器：version -> [fn(data) -> Optional[str]（返回错误信息或 None）]
        self._validators: Dict[int, List[Callable[[Dict], Optional[str]]]] = {}
        self._register_builtin_migrations()

    # ------------------------------------------------------------------ #
    # 内置迁移步骤与规则
    # ------------------------------------------------------------------ #
    def _register_builtin_migrations(self):
        # schema: v1 -> v2 增加 mp / inventory
        self.register_schema_migration(
            1, 2, "add_mp_and_inventory",
            lambda d: {**d, "mp": d.get("mp", 50), "inventory": d.get("inventory", [])},
        )
        # schema: v2 -> v3 增加 skills / achievements（依赖 v1->v2 先完成）
        self.register_schema_migration(
            2, 3, "add_skills_and_achievements",
            lambda d: {**d, "skills": d.get("skills", []), "achievements": d.get("achievements", [])},
            requires=["add_mp_and_inventory"],
        )
        # 数据规则：v1 hp 是 0-1 的比例，v2 起是绝对值
        self.register_data_rule(1, 2, self._convert_v1_to_v2_data)
        self.register_data_rule(2, 3, self._convert_v2_to_v3_data)
        # 验证器
        self.register_validator(2, self._validate_common)
        self.register_validator(3, self._validate_common)

    @staticmethod
    def _convert_v1_to_v2_data(data: Dict) -> Dict:
        out = dict(data)
        hp = out.get("hp")
        if isinstance(hp, float) and 0.0 <= hp <= 1.0:
            out["hp"] = int(round(hp * 100))
        return out

    @staticmethod
    def _convert_v2_to_v3_data(data: Dict) -> Dict:
        out = dict(data)
        # v3 中 inventory 由物品名列表改为 {"name": ..., "count": ...} 字典列表
        inventory = out.get("inventory")
        if isinstance(inventory, list):
            out["inventory"] = [
                {"name": item, "count": 1} if isinstance(item, str) else item
                for item in inventory
            ]
        return out

    @staticmethod
    def _validate_common(data: Dict) -> Optional[str]:
        if not isinstance(data.get("level"), int) or data["level"] < 1:
            return "level 必须是 >= 1 的整数"
        if not isinstance(data.get("hp"), int) or data["hp"] < 0:
            return "hp 必须是非负整数"
        return None

    # ------------------------------------------------------------------ #
    # 注册接口
    # ------------------------------------------------------------------ #
    def register_schema_migration(self, from_version: int, to_version: int,
                                  name: str, fn: Callable[[Dict], Dict],
                                  requires: Optional[List[str]] = None):
        self._schema_steps.setdefault(from_version, []).append({
            "name": name, "to": to_version, "fn": fn,
            "requires": list(requires or []),
        })

    def register_data_rule(self, from_version: int, to_version: int,
                           fn: Callable[[Dict], Dict]):
        self._data_rules[(from_version, to_version)] = fn

    def register_validator(self, version: int,
                           fn: Callable[[Dict], Optional[str]]):
        self._validators.setdefault(version, []).append(fn)

    # ------------------------------------------------------------------ #
    # 版本检测：容错 + 向前兼容
    # ------------------------------------------------------------------ #
    def detect_version(self, save_data) -> Optional[int]:
        """检测存档版本。

        - 容错：损坏/非法输入返回 None，绝不抛异常。
        - 向前兼容：比当前版本新的存档也能识别（返回其版本号）。
        """
        try:
            if not isinstance(save_data, dict):
                return None
            version = save_data.get("version")
            if isinstance(version, bool):
                return None
            if isinstance(version, int):
                return version if version >= 1 else None
            if isinstance(version, str):
                try:
                    parsed = int(version)
                    return parsed if parsed >= 1 else None
                except ValueError:
                    return None
            # 无显式版本号：根据字段集合推断（仅限已知版本）
            keys = set(save_data.keys())
            best = None
            for v, fields in sorted(SCHEMA_FIELDS.items()):
                if set(fields) <= keys:
                    best = v
            return best
        except Exception:
            return None

    def is_newer_version(self, save_data) -> bool:
        version = self.detect_version(save_data)
        return version is not None and version > self.current_version

    # ------------------------------------------------------------------ #
    # schema 迁移：拓扑排序 + 事务
    # ------------------------------------------------------------------ #
    def _ordered_steps(self, from_version: int, to_version: int) -> List[Dict]:
        """按依赖关系（拓扑序）展开 from_version -> to_version 的迁移链。"""
        chain: List[Dict] = []
        version = from_version
        while version < to_version:
            steps = self._schema_steps.get(version)
            if not steps:
                raise MigrationError(f"缺少 {version} -> {version + 1} 的 schema 迁移")
            chain.extend(steps)
            version = max(s["to"] for s in steps)
        return self._topo_sort(chain)

    @staticmethod
    def _topo_sort(steps: List[Dict]) -> List[Dict]:
        ordered: List[Dict] = []
        done = set()
        remaining = list(steps)
        while remaining:
            progressed = False
            for step in list(remaining):
                if all(req in done for req in step["requires"]):
                    ordered.append(step)
                    done.add(step["name"])
                    remaining.remove(step)
                    progressed = True
            if not progressed:
                raise MigrationError("schema 迁移存在循环依赖")
        return ordered

    def migrate_schema(self, save_data: Dict,
                       target_version: Optional[int] = None) -> MigrationResult:
        """schema 迁移：按依赖顺序执行，整体事务（失败即回滚，原数据不变）。"""
        target = target_version if target_version is not None else self.current_version
        from_version = self.detect_version(save_data)
        if from_version is None:
            return MigrationResult(False, errors=["无法识别存档版本"])
        if from_version >= target:
            return MigrationResult(True, from_version, target,
                                   data=copy.deepcopy(save_data))
        # 事务：在副本上执行，全部成功才提交
        working = copy.deepcopy(save_data)
        applied: List[str] = []
        try:
            for step in self._ordered_steps(from_version, target):
                working = step["fn"](working)
                if not isinstance(working, dict):
                    raise MigrationError(f"迁移步骤 {step['name']} 返回了非法数据")
                applied.append(step["name"])
            working["version"] = target
        except Exception as exc:
            self._history.append({
                "op": "migrate_schema", "from": from_version, "to": target,
                "success": False, "applied": applied, "error": str(exc),
            })
            return MigrationResult(False, from_version, target,
                                   errors=[f"schema 迁移失败，已回滚: {exc}"])
        self._history.append({
            "op": "migrate_schema", "from": from_version, "to": target,
            "success": True, "applied": applied,
        })
        return MigrationResult(True, from_version, target, data=working)

    # ------------------------------------------------------------------ #
    # 数据迁移：转换规则链 + 验证
    # ------------------------------------------------------------------ #
    def data_migration(self, save_data: Dict,
                       target_version: Optional[int] = None,
                       from_version: Optional[int] = None) -> MigrationResult:
        """数据迁移：按版本链逐步应用转换规则，迁移后执行验证。"""
        target = target_version if target_version is not None else self.current_version
        if from_version is None:
            from_version = self.detect_version(save_data)
        if from_version is None:
            return MigrationResult(False, errors=["无法识别存档版本"])
        working = copy.deepcopy(save_data)
        version = from_version
        try:
            while version < target:
                rule = self._data_rules.get((version, version + 1))
                if rule is None:
                    raise MigrationError(f"缺少 {version} -> {version + 1} 的数据转换规则")
                working = rule(working)
                if not isinstance(working, dict):
                    raise MigrationError(f"数据规则 {version}->{version + 1} 返回了非法数据")
                version += 1
            working["version"] = target
        except Exception as exc:
            return MigrationResult(False, from_version, target,
                                   errors=[f"数据迁移失败: {exc}"])
        # 验证迁移结果
        errors = [err for validate in self._validators.get(target, [])
                  for err in [validate(working)] if err]
        if errors:
            return MigrationResult(False, from_version, target, errors=errors)
        self._history.append({
            "op": "data_migration", "from": from_version, "to": target,
            "success": True,
        })
        return MigrationResult(True, from_version, target, data=working)

    # ------------------------------------------------------------------ #
    # 回滚：迁移前备份 + 版本记录
    # ------------------------------------------------------------------ #
    def backup(self, save_data: Dict) -> Dict:
        """迁移前备份原存档，并记录版本号。"""
        record = {
            "version": self.detect_version(save_data),
            "data": copy.deepcopy(save_data),
        }
        self._backups.append(record)
        return record

    def migrate_save(self, save_data: Dict,
                     target_version: Optional[int] = None) -> MigrationResult:
        """完整迁移流程：备份 -> schema 迁移 -> 数据迁移。

        迁移前自动备份；任一步失败时备份保留在栈上，调用方可 rollback() 恢复。
        """
        self.backup(save_data)
        original_version = self.detect_version(save_data)
        schema_result = self.migrate_schema(save_data, target_version)
        if not schema_result.success:
            return schema_result
        data_result = self.data_migration(schema_result.data, target_version,
                                          from_version=original_version)
        if not data_result.success:
            return data_result
        return data_result

    def rollback(self) -> Optional[Dict]:
        """回滚到最近一次备份的存档（含版本记录）。"""
        if not self._backups:
            return None
        record = self._backups.pop()
        restored = copy.deepcopy(record["data"])
        self._state["current_save"] = restored
        self._history.append({
            "op": "rollback", "to_version": record["version"], "success": True,
        })
        return restored

    @property
    def backups(self) -> List[Dict]:
        return list(self._backups)

    # ------------------------------------------------------------------ #
    # 兼容性处理：默认值 + 警告
    # ------------------------------------------------------------------ #
    def compatibility(self, save_data: Dict) -> Tuple[Optional[Dict], List[str]]:
        """让当前版本能安全读取老存档。

        返回 (补齐默认值后的存档, 警告列表)；无法识别时返回 (None, 警告)。
        """
        warnings: List[str] = []
        version = self.detect_version(save_data)
        if version is None:
            return None, ["存档损坏或格式无法识别"]
        if version > self.current_version:
            warnings.append(
                f"存档版本 v{version} 高于当前支持的 v{self.current_version}，"
                "将尽力读取，数据可能不完整"
            )
        elif version < self.current_version:
            warnings.append(
                f"存档来自老版本 v{version}（当前 v{self.current_version}），"
                "建议执行迁移后再游玩"
            )
        data = copy.deepcopy(save_data)
        known_fields = SCHEMA_FIELDS.get(version, SCHEMA_FIELDS.get(self.current_version, ()))
        for field_name in known_fields:
            if field_name not in data:
                data[field_name] = _default_for(field_name)
                warnings.append(f"字段 {field_name} 缺失，已填充默认值")
        return data, warnings

    # ------------------------------------------------------------------ #
    # 既有接口
    # ------------------------------------------------------------------ #
    def update(self, dt: float):
        pass

    def reset(self):
        self._state = {}
        self._history = []
        self._backups = []
