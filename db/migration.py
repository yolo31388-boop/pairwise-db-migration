"""
游戏内存档迁移与版本升级系统 - 核心模块

功能：
- detect_version(): 版本检测（容错 + 向前兼容）
- migrate_schema(): schema 迁移（按依赖顺序执行 + 事务保护）
- data_migration(): 数据迁移（转换规则 + 结果验证）
- rollback(): 回滚（迁移前自动备份 + 版本记录）
- compatibility(): 兼容性处理（缺失字段补默认值 + 老版本警告）
"""
import copy
import warnings
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

CURRENT_VERSION = 3


class MigrationError(Exception):
    """迁移过程中的任何失败都抛出此异常，原始存档保证不被破坏。"""


@dataclass
class Config:
    pass


# ---------------------------------------------------------------------------
# 存档格式演进
#   v1: {"version":1, "player":{"name","hp"(str)}, "items":[str]}
#   v2: items -> [{"name","qty"}], player 增加 "mana", 新增 "gold_copper"
#   v3: "gold_copper" 更名为 "gold"(单位:金, 100铜=1金), 新增 "level", hp 为 int
# ---------------------------------------------------------------------------

CURRENT_DEFAULTS = {
    "player": {"name": "Unknown", "hp": 100, "mana": 0},
    "items": [],
    "gold": 0,
    "level": 1,
}


def _schema_1_to_2(save: Dict) -> Dict:
    """物品列表从纯名字升级为 {name, qty}，player 增加 mana。"""
    items = save.get("items", [])
    save["items"] = [
        {"name": item, "qty": 1} if isinstance(item, str) else item
        for item in items
    ]
    player = save.setdefault("player", {})
    player.setdefault("mana", 0)
    return save


def _schema_2_to_3(save: Dict) -> Dict:
    """gold_copper 更名为 gold，新增 level。依赖 1->2 提供的 mana 字段。"""
    player = save.get("player")
    if not isinstance(player, dict) or "mana" not in player:
        raise MigrationError("schema 2->3 依赖 1->2 迁移生成的 player.mana 字段")
    if "gold_copper" in save:
        save["gold"] = save.pop("gold_copper")
    save.setdefault("gold", 0)
    save.setdefault("level", 1)
    return save


def _data_1_to_2(save: Dict) -> Dict:
    """v1 的 hp 是字符串，转换为整数。"""
    player = save.get("player", {})
    hp = player.get("hp")
    if isinstance(hp, str):
        player["hp"] = int(hp)
    return save


def _data_2_to_3(save: Dict) -> Dict:
    """铜币换算为金币（100 铜 = 1 金）。"""
    gold = save.get("gold", 0)
    if isinstance(gold, bool) or not isinstance(gold, (int, float)):
        raise MigrationError("gold 字段类型非法，无法换算")
    save["gold"] = round(gold / 100, 2)
    return save


class DatabaseMigration:
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._state = {}
        self._history = []
        # schema 迁移步骤表：key 为起始版本，链式解析保证执行顺序
        self.schema_migrations: Dict[int, Callable[[Dict], Dict]] = {
            1: _schema_1_to_2,
            2: _schema_2_to_3,
        }
        # 数据转换规则表：key 为 (起始版本, 目标版本)
        self.data_rules: Dict[Tuple[int, int], Callable[[Dict], Dict]] = {
            (1, 2): _data_1_to_2,
            (2, 3): _data_2_to_3,
        }
        # 回滚支持：备份栈 + 版本记录
        self._backups: List[Tuple[int, Dict]] = []
        self._version_log: List[int] = []
        # 兼容性警告记录
        self.warnings: List[str] = []

    # ------------------------------------------------------------------
    # 版本检测
    # ------------------------------------------------------------------
    def detect_version(self, save) -> int:
        """检测存档版本。

        - 容错：损坏/非法存档返回 -1，绝不抛异常；
        - 向前兼容：高于当前版本的存档返回真实版本号，可继续读取。
        """
        try:
            if not isinstance(save, dict):
                return -1
            version = save.get("version")
            if isinstance(version, bool):
                return -1
            if isinstance(version, int):
                return version if version > 0 else -1
            if isinstance(version, str) and version.strip().isdigit():
                return int(version.strip())
            return -1
        except Exception:
            return -1

    def is_compatible(self, save) -> bool:
        return self.detect_version(save) > 0

    # ------------------------------------------------------------------
    # schema 迁移：按版本链顺序执行 + 事务保护
    # ------------------------------------------------------------------
    def migrate_schema(self, save: Dict, target_version: Optional[int] = None) -> Dict:
        target = target_version if target_version is not None else CURRENT_VERSION
        version = self.detect_version(save)
        if version < 0:
            raise MigrationError("无法识别的存档，schema 迁移中止")
        if version > target:
            # 向前兼容：高版本存档不做降级，原样返回副本
            return copy.deepcopy(save)

        self._backup(save)
        working = copy.deepcopy(save)  # 事务：所有修改作用于副本
        try:
            while version < target:
                step = self.schema_migrations.get(version)
                if step is None:
                    raise MigrationError(f"缺少从版本 {version} 到 {version + 1} 的 schema 迁移步骤")
                working = step(working)
                version += 1
                working["version"] = version
                self._version_log.append(version)
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(f"schema 迁移失败（版本 {version}），原存档未受影响: {exc}") from exc
        return working

    # ------------------------------------------------------------------
    # 数据迁移：转换规则 + 结果验证
    # ------------------------------------------------------------------
    def data_migration(self, save: Dict, from_version: Optional[int] = None,
                       to_version: Optional[int] = None) -> Dict:
        source = from_version if from_version is not None else self.detect_version(save)
        target = to_version if to_version is not None else CURRENT_VERSION
        if source < 0:
            raise MigrationError("无法识别的存档，数据迁移中止")

        self._backup(save)
        working = copy.deepcopy(save)
        try:
            version = source
            while version < target:
                rule = self.data_rules.get((version, version + 1))
                if rule is not None:
                    working = rule(working)
                version += 1
                working["version"] = version
            self.validate(working, target)
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(f"数据迁移失败，原存档未受影响: {exc}") from exc
        return working

    def validate(self, save: Dict, version: Optional[int] = None) -> bool:
        """验证迁移结果是否符合目标版本的数据格式。"""
        target = version if version is not None else CURRENT_VERSION
        if target < CURRENT_VERSION:
            return True
        player = save.get("player")
        if not isinstance(player, dict):
            raise MigrationError("验证失败：缺少 player 字段")
        hp = player.get("hp")
        if isinstance(hp, bool) or not isinstance(hp, int):
            raise MigrationError("验证失败：player.hp 必须是整数")
        mana = player.get("mana")
        if isinstance(mana, bool) or not isinstance(mana, int):
            raise MigrationError("验证失败：player.mana 必须是整数")
        items = save.get("items")
        if not isinstance(items, list):
            raise MigrationError("验证失败：items 必须是列表")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                    or not isinstance(item.get("qty"), int):
                raise MigrationError("验证失败：items 元素必须是 {name: str, qty: int}")
        gold = save.get("gold")
        if isinstance(gold, bool) or not isinstance(gold, (int, float)) or gold < 0:
            raise MigrationError("验证失败：gold 必须是非负数值")
        level = save.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or level < 1:
            raise MigrationError("验证失败：level 必须是 >= 1 的整数")
        return True

    # ------------------------------------------------------------------
    # 完整迁移流程
    # ------------------------------------------------------------------
    def migrate(self, save: Dict) -> Dict:
        version = self.detect_version(save)
        if version < 0:
            raise MigrationError("存档损坏或格式无法识别，迁移中止")
        if version > CURRENT_VERSION:
            return self.compatibility(save)
        migrated = self.migrate_schema(save)
        migrated = self.data_migration(migrated, from_version=version)
        return migrated

    # ------------------------------------------------------------------
    # 回滚：迁移前自动备份 + 版本记录
    # ------------------------------------------------------------------
    def _backup(self, save: Dict):
        version = self.detect_version(save)
        self._backups.append((version, copy.deepcopy(save)))
        if not self._version_log:
            self._version_log.append(version)

    def get_backup(self, version: Optional[int] = None) -> Optional[Dict]:
        """获取指定版本的备份快照；不指定则返回最近一次备份。"""
        for backup_version, snapshot in reversed(self._backups):
            if version is None or backup_version == version:
                return copy.deepcopy(snapshot)
        return None

    @property
    def version_history(self) -> List[int]:
        return list(self._version_log)

    def rollback(self) -> Dict:
        """回滚到最近一次备份的版本，并记录回滚目标版本。"""
        if not self._backups:
            raise MigrationError("没有可用的备份，无法回滚")
        version, snapshot = self._backups.pop()
        self._version_log.append(version)
        return copy.deepcopy(snapshot)

    # ------------------------------------------------------------------
    # 兼容性处理：缺失字段补默认值 + 老版本/新版本警告
    # ------------------------------------------------------------------
    def compatibility(self, save: Dict) -> Dict:
        version = self.detect_version(save)
        if version < 0:
            raise MigrationError("存档损坏或格式无法识别")
        result = copy.deepcopy(save)
        if version > CURRENT_VERSION:
            self._warn(f"存档版本 v{version} 高于当前支持的 v{CURRENT_VERSION}，"
                       f"已按向前兼容模式读取，未知字段将被保留")
        elif version < CURRENT_VERSION:
            self._warn(f"存档版本 v{version} 低于当前版本 v{CURRENT_VERSION}，"
                       f"缺失字段已填充默认值，建议执行迁移")
        self._apply_defaults(result, CURRENT_DEFAULTS)
        return result

    def _warn(self, message: str):
        self.warnings.append(message)
        warnings.warn(message, UserWarning, stacklevel=3)

    def _apply_defaults(self, target: Dict, defaults: Dict):
        for key, value in defaults.items():
            if key not in target:
                target[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                self._apply_defaults(target[key], value)

    # ------------------------------------------------------------------
    # 原有接口保持不变
    # ------------------------------------------------------------------
    def update(self, dt: float):
        pass

    def reset(self):
        self._state = {}
        self._history = []
        self._backups = []
        self._version_log = []
        self.warnings = []
