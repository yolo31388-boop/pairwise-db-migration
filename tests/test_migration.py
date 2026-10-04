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

from db.migration import (
    DatabaseMigration, MigrationError, MigrationResult,
)


class TestDatabaseMigration:
    def setup_method(self):
        self.system = DatabaseMigration()
        self.save_v1 = {"version": 1, "player_name": "Hero", "level": 5, "hp": 0.8}
        self.save_v2 = {
            "version": 2, "player_name": "Hero", "level": 5, "hp": 80,
            "mp": 30, "inventory": ["sword", "potion"],
        }
        self.save_v3 = {
            "version": 3, "player_name": "Hero", "level": 5, "hp": 80,
            "mp": 30, "inventory": [{"name": "sword", "count": 1}],
            "skills": ["fireball"], "achievements": ["first_blood"],
        }

    # ---- 版本检测 ----
    def test_case_01(self):
        """显式版本号能被准确检测出来。"""
        assert self.system.detect_version(self.save_v1) == 1
        assert self.system.detect_version(self.save_v2) == 2
        assert self.system.detect_version(self.save_v3) == 3

    def test_case_02(self):
        """没有版本号时能根据字段集合推断版本。"""
        data = {"player_name": "Hero", "level": 2, "hp": 100, "mp": 10,
                "inventory": [], "skills": [], "achievements": []}
        assert self.system.detect_version(data) == 3
        old = {"player_name": "Hero", "level": 1, "hp": 100}
        assert self.system.detect_version(old) == 1

    def test_case_03(self):
        """损坏的存档不崩溃，返回 None。"""
        assert self.system.detect_version(None) is None
        assert self.system.detect_version("broken data") is None
        assert self.system.detect_version(b"corrupt") is None
        assert self.system.detect_version({"version": -2}) is None
        assert self.system.detect_version({"version": "abc"}) is None

    def test_case_04(self):
        """向前兼容：比当前版本新的存档也能识别并标记。"""
        future_save = {"version": 99, "player_name": "TimeTraveler"}
        assert self.system.detect_version(future_save) == 99
        assert self.system.is_newer_version(future_save) is True
        assert self.system.is_newer_version(self.save_v1) is False

    # ---- schema 迁移 ----
    def test_case_05(self):
        """schema 迁移后老版本的新字段在新版本中可读取。"""
        result = self.system.migrate_schema(self.save_v1, 3)
        assert result.success is True
        data = result.data
        assert data["version"] == 3
        assert "mp" in data and "inventory" in data
        assert "skills" in data and "achievements" in data

    def test_case_06(self):
        """迁移步骤按依赖顺序执行，依赖未满足的步骤不会先跑。"""
        order = []

        def step_a(d):
            order.append("a")
            d["field_a"] = 1
            return d

        def step_b(d):
            assert "field_a" in d, "依赖 a 必须先执行"
            order.append("b")
            d["field_b"] = d["field_a"] + 1
            return d

        system = DatabaseMigration()
        system.register_schema_migration(1, 2, "a", step_a)
        system.register_schema_migration(2, 3, "b", step_b, requires=["a"])
        result = system.migrate_schema({"version": 1}, 3)
        assert result.success is True
        assert order == ["a", "b"]
        assert result.data["field_b"] == 2

    def test_case_07(self):
        """schema 迁移是事务：中途失败时原存档完全不变。"""
        def boom(d):
            raise RuntimeError("disk full")

        system = DatabaseMigration()
        system.register_schema_migration(2, 3, "boom", boom)
        original = copy.deepcopy(self.save_v2)
        result = system.migrate_schema(self.save_v2, 3)
        assert result.success is False
        assert self.save_v2 == original  # 传入的数据未被污染

    def test_case_08(self):
        """循环依赖被检测，且不会执行任何破坏性操作。"""
        system = DatabaseMigration()
        system.register_schema_migration(1, 2, "x", lambda d: d, requires=["y"])
        system.register_schema_migration(2, 3, "y", lambda d: d, requires=["x"])
        result = system.migrate_schema({"version": 1}, 3)
        assert result.success is False
        assert "依赖" in result.errors[0]

    # ---- 数据迁移 ----
    def test_case_09(self):
        """数据迁移按转换规则把老格式数据转成新格式。"""
        result = self.system.data_migration(self.save_v2, 3)
        assert result.success is True
        assert result.data["inventory"] == [
            {"name": "sword", "count": 1},
            {"name": "potion", "count": 1},
        ]

    def test_case_10(self):
        """跨多个版本时转换规则链逐版本执行。"""
        result = self.system.migrate_save(self.save_v1, 3)
        assert result.success is True
        assert result.data["hp"] == 80  # 0.8 * 100
        assert result.data["inventory"] == []
        assert result.data["version"] == 3

    def test_case_11(self):
        """迁移后数据不合法时验证失败。"""
        bad_save = {"version": 2, "player_name": "Hero", "level": -3, "hp": 80,
                    "mp": 30, "inventory": []}
        result = self.system.data_migration(bad_save, 3)
        assert result.success is False
        assert any("level" in err for err in result.errors)

    # ---- 回滚 ----
    def test_case_12(self):
        """完整迁移流程在迁移前自动备份原存档。"""
        self.system.backup(self.save_v1)
        backups = self.system.backups
        assert len(backups) == 1
        assert backups[0]["version"] == 1
        assert backups[0]["data"] == self.save_v1
        assert backups[0]["data"] is not self.save_v1  # 深拷贝，互不影响

    def test_case_13(self):
        """迁移失败后 rollback 能恢复原存档。"""
        def boom(d):
            raise RuntimeError("migration crashed")

        system = DatabaseMigration()
        system.register_schema_migration(2, 3, "boom", boom)
        result = system.migrate_save({"version": 2, "player_name": "Hero",
                                      "level": 3, "hp": 50, "mp": 10,
                                      "inventory": []}, 3)
        assert result.success is False
        restored = system.rollback()
        assert restored is not None
        assert restored["version"] == 2
        assert restored["hp"] == 50

    def test_case_14(self):
        """没有备份可回滚时返回 None，版本记录用于决定回滚目标。"""
        assert self.system.rollback() is None
        self.system.backup(self.save_v2)
        restored = self.system.rollback()
        assert restored is not None
        assert restored["version"] == 2

    # ---- 兼容性 ----
    def test_case_15(self):
        """兼容处理为缺失字段填默认值，并对老版本/未来版本给出警告。"""
        incomplete_old = {"version": 1, "player_name": "Hero", "level": 4}
        data, warnings = self.system.compatibility(incomplete_old)
        assert data["hp"] == 100  # 缺失字段填默认值，不报错
        assert any("老版本" in w for w in warnings)
        assert any("hp" in w for w in warnings)

        future, future_warnings = self.system.compatibility(
            {"version": 99, "player_name": "T"})
        assert any("高于当前" in w for w in future_warnings)
        assert future["player_name"] == "T"

        bad_data, bad_warnings = self.system.compatibility("garbage")
        assert bad_data is None
        assert bad_warnings  # 损坏存档也有明确提示


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
