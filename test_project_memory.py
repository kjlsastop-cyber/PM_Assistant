# -*- coding: utf-8 -*-
"""Project Memory MVP 闭环测试脚本（不依赖 Streamlit）。

流程：创建测试项目 → 输入示例会议纪要 → LLM 抽取 → 写入 Supabase → get_project_snapshot → 终端打印最终项目状态。

运行：python test_project_memory.py
"""
from __future__ import annotations

import sys

import project_memory as pm
from fact_extractor import ExtractionError, extract_and_save

# 示例会议纪要（模拟真实周报/例会文本）
SAMPLE_MINUTES = """
药监局申报助手项目 — 周例会纪要（2026-08-28）

参会人员：张三（项目经理）、李四（产品）、王五（开发）、赵六（测试）

一、本周决议
1. 决定采用 Supabase 作为项目状态数据库，替换原计划的本地 SQLite。（决策人：张三）
2. 需求评审会定在 2026-09-01（下周二）召开。

二、任务进展
- 数据库表结构设计已完成。（负责人：王五）
- 知识库分块逻辑优化正在进行中，预计 2026-09-03 前完成。（负责人：李四）
- PPT 导出功能尚未开始，依赖需求评审结果。（负责人：王五）
- 接口联调被阻塞：等待第三方接口文档。（负责人：赵六）

三、风险
1. 第三方接口文档迟迟未提供，可能拖慢整体联调进度，风险等级高。（负责人：赵六）
2. 测试环境资源不足，可能需要申请新服务器，风险等级中。

四、重要事件
- 2026-08-25 完成第一轮内部演示。
- 2026-08-28 召开本次周例会。
"""

LINE = "-" * 60


def _fmt_date(v):
    return str(v) if v else "—"


def _fmt_status(status: str) -> str:
    return {
        pm.TASK_STATUS_TODO: "待办",
        pm.TASK_STATUS_IN_PROGRESS: "进行中",
        pm.TASK_STATUS_BLOCKED: "阻塞",
        pm.TASK_STATUS_DONE: "已完成",
    }.get(status, status)


def print_snapshot(snap: dict):
    """把快照以可读格式打印到终端。"""
    project = snap.get("project") or {}
    print(LINE)
    print(f"项目：{project.get('name')}（状态：{project.get('status')}）")
    print(f"项目 ID：{project.get('id')}")
    print(LINE)

    print(f"\n【当前任务】（{len(snap.get('current_tasks', []))}）")
    for t in snap.get("current_tasks", []):
        print(f"  - [{_fmt_status(t['status'])}] {t['title']}（负责人：{t.get('owner') or '—'}，截止：{_fmt_date(t.get('due_date'))}）")

    print(f"\n【阻塞任务】（{len(snap.get('blocked_tasks', []))}）")
    for t in snap.get("blocked_tasks", []):
        print(f"  - {t['title']}（负责人：{t.get('owner') or '—'}，依赖：{t.get('dependency') or '—'}）")

    print(f"\n【已完成任务】（{len(snap.get('completed_tasks', []))}）")
    for t in snap.get("completed_tasks", []):
        print(f"  - {t['title']}（负责人：{t.get('owner') or '—'}）")

    print(f"\n【最新决策】（{len(snap.get('latest_decisions', []))}）")
    for d in snap.get("latest_decisions", []):
        print(f"  - {d['content']}（决策人：{d.get('decision_maker') or '—'}，日期：{_fmt_date(d.get('decision_date'))}）")

    print(f"\n【未关闭风险】（{len(snap.get('open_risks', []))}）")
    for r in snap.get("open_risks", []):
        print(f"  - [{r.get('severity') or '未评级'}] {r['content']}（负责人：{r.get('owner') or '—'}）")

    print(f"\n【最近事件】（{len(snap.get('recent_events', []))}）")
    for e in snap.get("recent_events", []):
        print(f"  - {_fmt_date(e.get('event_date'))} {e['content']}")
    print(LINE)


def main() -> int:
    # 1. 检查数据库
    print("步骤 1/5：检查数据库连接 ...")
    if not pm.database_available():
        print("❌ 数据库不可用：请检查 .env 中的 DATABASE_URL 或 DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD")
        return 1
    pm.init_db()
    print("✅ 数据库连接正常，表结构就绪")

    # 2. 创建测试项目
    print("\n步骤 2/5：创建测试项目 ...")
    project_id = pm.create_project("药监局申报助手（测试）", description="test_project_memory.py 自动创建的测试项目")
    if project_id is None:
        print("❌ 测试项目创建失败（数据库写入异常）")
        return 1
    print(f"✅ 项目已创建，id = {project_id}")

    # 3 + 4. LLM 抽取并写入数据库
    print("\n步骤 3/5：调用 LLM 从示例会议纪要中抽取事实 ...")
    print("步骤 4/5：将抽取结果写入 Supabase ...")
    try:
        facts, ids = extract_and_save(project_id, SAMPLE_MINUTES, source="周例会纪要-2026-08-28")
    except ExtractionError as exc:
        print(f"❌ {exc}")
        return 1
    print(
        "✅ 写入完成：决策 {d} 条，任务 {t} 条，风险 {r} 条，事件 {e} 条".format(
            d=len(ids["decisions"]), t=len(ids["tasks"]), r=len(ids["risks"]), e=len(ids["events"])
        )
    )

    # 5. 快照 + 打印
    print("\n步骤 5/5：读取项目快照 ...")
    snap = pm.get_project_snapshot(project_id)
    if not snap.get("project"):
        print("❌ 快照读取失败：项目不存在或数据库异常")
        return 1
    print_snapshot(snap)

    print("\n🎉 MVP 闭环测试通过。测试数据已保留在数据库中，可在 Supabase 后台查看。")
    print(f"   如需清理：在 SQL Editor 执行 DELETE FROM projects WHERE id = '{project_id}';（子表级联删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
