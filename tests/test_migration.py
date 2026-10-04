"""
游戏内存档迁移与版本升级系统 - 验收测试
运行方式：python -m pytest tests/test_migration.py -q
共 15 个测试用例
"""
import copy

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migration import CURRENT_VERSION, DatabaseMigration, MigrationError


def make_v1():
    return {
        "version": 1,
        "player": {"name": "Hero", "hp": "100"},
        "items": ["sword", "potion"],
    }


def make_v2():
    return {
        "version": 2,
        "player": {"name": "Hero", "hp": 100, "mana": 50},
        "items": [{"name": "sword", "qty": 1}],
        "gold_copper": 250,
    }


def make_v3():
    return {
        "version": 3,
        "player": {"name": "Hero", "hp": 100, "mana": 50},
        "items": [{"name": "sword", "qty": 1}],
        "gold": 2.5,
        "level": 3,
    }


class TestDatabaseMigration:
    def setup_method(self):
        self.system = DatabaseMigration()

    # ---------------- 版本检测 ----------------

    def test_case_01(self):
        """版本检测实际生效：能识别各版本存档"""
        assert self.system.detect_version(make_v1()) == 1
        assert self.system.detect_version(make_v2()) == 2
        assert self.system.detect_version(make_v3()) == 3
        assert self.system.detect_version({"version": "2"}) == 2

    def test_case_02(self):
        """版本检测容错：损坏存档返回 -1 而不是崩溃"""
        for broken in (None, "garbage", 42, [], {}, {"version": "abc"},
                       {"version": None}, {"version": True}, {"version": -1}):
            assert self.system.detect_version(broken) == -1

    def test_case_03(self):
        """版本检测向前兼容：比当前版本新的存档也能识别和读取"""
        future = make_v3()
        future["version"] = CURRENT_VERSION + 5
        future["new_field"] = "from_the_future"
        assert self.system.detect_version(future) == CURRENT_VERSION + 5
        with pytest.warns(UserWarning):
            loaded = self.system.compatibility(future)
        assert loaded["new_field"] == "from_the_future"
        assert loaded["player"]["name"] == "Hero"

    # ---------------- schema 迁移 ----------------

    def test_case_04(self):
        """schema 迁移实际生效：v1 老字段迁移到 v3 后可读"""
        migrated = self.system.migrate_schema(make_v1())
        assert migrated["version"] == CURRENT_VERSION
        assert migrated["items"] == [
            {"name": "sword", "qty": 1},
            {"name": "potion", "qty": 1},
        ]
        assert migrated["player"]["mana"] == 0
        assert migrated["level"] == 1

    def test_case_05(self):
        """schema 迁移有顺序：按版本链执行，依赖前序步骤的字段"""
        executed = []
        for version, step in list(self.system.schema_migrations.items()):
            self.system.schema_migrations[version] = (
                lambda save, v=version, s=step: (executed.append(v), s(save))[1]
            )
        self.system.migrate_schema(make_v1())
        assert executed == [1, 2]
        # 2->3 依赖 1->2 生成的 mana 字段，缺失则报错
        broken_v2 = make_v2()
        del broken_v2["player"]["mana"]
        with pytest.raises(MigrationError):
            self.system.migrate_schema(broken_v2)

    def test_case_06(self):
        """schema 迁移有事务：中途失败时原存档不被破坏"""
        save = make_v1()
        original = copy.deepcopy(save)

        def boom(s):
            s["corrupted"] = True
            raise RuntimeError("disk error")

        self.system.schema_migrations[2] = boom
        with pytest.raises(MigrationError):
            self.system.migrate_schema(save)
        assert save == original

    # ---------------- 数据迁移 ----------------

    def test_case_07(self):
        """数据迁移实际生效：老格式数据迁移后在新版本正确"""
        migrated = self.system.migrate(make_v1())
        assert migrated["player"]["hp"] == 100
        assert isinstance(migrated["player"]["hp"], int)
        assert self.system.validate(migrated)

    def test_case_08(self):
        """数据迁移有转换规则：铜币按规则换算为金币"""
        migrated = self.system.migrate(make_v2())
        assert migrated["gold"] == 2.5
        assert "gold_copper" not in migrated

    def test_case_09(self):
        """数据迁移有验证：迁移结果不合法时报错且原存档不变"""
        bad = make_v3()
        bad["player"]["hp"] = "not_a_number"
        original = copy.deepcopy(bad)
        with pytest.raises(MigrationError):
            self.system.data_migration(bad, from_version=2, to_version=3)
        assert bad == original

    # ---------------- 回滚 ----------------

    def test_case_10(self):
        """回滚实际生效：迁移失败后可恢复到原存档"""
        save = make_v1()
        original = copy.deepcopy(save)
        self.system.schema_migrations[2] = lambda s: 1 / 0
        with pytest.raises(MigrationError):
            self.system.migrate_schema(save)
        restored = self.system.rollback()
        assert restored == original

    def test_case_11(self):
        """回滚有备份：迁移前自动保存原存档"""
        original = make_v1()
        self.system.migrate_schema(original)
        backup = self.system.get_backup(version=1)
        assert backup == original
        # 空系统没有备份，回滚报错而不是丢数据
        with pytest.raises(MigrationError):
            DatabaseMigration().rollback()

    def test_case_12(self):
        """回滚有版本记录：知道每个版本变迁和回滚目标"""
        self.system.migrate_schema(make_v1())
        assert self.system.version_history == [1, 2, 3]
        restored = self.system.rollback()
        assert restored["version"] == 1
        assert self.system.version_history[-1] == 1

    # ---------------- 兼容性处理 ----------------

    def test_case_13(self):
        """兼容性处理实际生效：新版本读老存档数据正确"""
        loaded = self.system.compatibility(make_v1())
        assert loaded["player"]["name"] == "Hero"
        assert loaded["items"] == ["sword", "potion"]
        # 完整迁移后的老存档能通过新版本验证
        assert self.system.validate(self.system.migrate(make_v1()))

    def test_case_14(self):
        """兼容性有默认值：缺失字段自动填充而不是报错"""
        incomplete = {"version": 3, "player": {"name": "Hero", "hp": 80}}
        loaded = self.system.compatibility(incomplete)
        assert loaded["player"]["mana"] == 0
        assert loaded["items"] == []
        assert loaded["gold"] == 0
        assert loaded["level"] == 1
        assert loaded["player"]["hp"] == 80

    def test_case_15(self):
        """兼容性有警告：读取老版本存档时明确提示玩家"""
        with pytest.warns(UserWarning):
            self.system.compatibility(make_v1())
        assert any("v1" in w for w in self.system.warnings)
        # 当前版本不告警
        self.system.reset()
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("error")
            self.system.compatibility(make_v3())
        assert self.system.warnings == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
