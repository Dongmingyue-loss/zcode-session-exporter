#!/usr/bin/env python3
"""Generate a synthetic demo SQLite database for zcode-session-exporter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "demo" / "demo.sqlite"


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        DROP TABLE IF EXISTS part;
        DROP TABLE IF EXISTS message;
        DROP TABLE IF EXISTS session;

        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            task_type TEXT,
            parent_id TEXT,
            slug TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT,
            sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT,
            sequence INTEGER
        );
        """
    )


def add_message(
    con: sqlite3.Connection,
    session_id: str,
    message_id: str,
    sequence: int,
    role: str,
    created: int,
    parts: list[dict],
    **extra,
) -> None:
    message = {
        "role": role,
        "time": {"created": created},
        "agent": "zcode-agent",
        **extra,
    }
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?,?)",
        (message_id, session_id, created, created, dumps(message), sequence),
    )
    for index, part in enumerate(parts):
        con.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
            (
                f"{message_id}_p{index}",
                message_id,
                session_id,
                created + index,
                created + index,
                dumps(part),
                index,
            ),
        )


def build(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    con = sqlite3.connect(output)
    try:
        create_schema(con)

        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
            (
                "sess_demo_config",
                "Demo: ZCode 配置迁移",
                r"D:\Demo\workspace",
                1790000000000,
                1790000600000,
                "interactive",
                None,
                "demo-config",
            ),
        )
        add_message(
            con,
            "sess_demo_config",
            "demo_m0",
            0,
            "user",
            1790000001000,
            [{"type": "text", "text": "帮我把 ZCode 的 provider、API Key 和 MCP 配置迁移到 DSH。"}],
        )
        add_message(
            con,
            "sess_demo_config",
            "demo_m1",
            1,
            "assistant",
            1790000002000,
            [
                {"type": "step-start"},
                {"type": "reasoning", "text": "先盘点源端配置，再生成迁移计划，最后写入目标 profile。"},
                {"type": "text", "text": "我会先运行 scan 和 plan，确认没有冲突后再 apply。"},
                {
                    "type": "tool",
                    "tool": "Bash",
                    "callID": "call_demo_1",
                    "state": {
                        "status": "completed",
                        "input": {"cmd": "python bin/zcode2dsh.py scan"},
                        "output": "providers: 18\nmcp: 2\nconflicts: 0\n",
                        "metadata": {"demo": True},
                    },
                },
                {
                    "type": "step-finish",
                    "reason": "tool-calls",
                    "tokens": {"input": 1200, "output": 180, "reasoning": 60},
                },
            ],
            modelID="demo-model",
            providerID="demo-provider",
            mode="plan",
            tokens={"input": 1200, "output": 180, "reasoning": 60},
            finish="tool-calls",
        )
        add_message(
            con,
            "sess_demo_config",
            "demo_m2",
            2,
            "user",
            1790000003000,
            [{"type": "text", "text": "可以，顺便把迁移报告保存下来。"}],
        )
        add_message(
            con,
            "sess_demo_config",
            "demo_m3",
            3,
            "assistant",
            1790000004000,
            [
                {
                    "type": "file",
                    "filename": "migration-report.json",
                    "mime": "application/json",
                    "url": r"D:\Demo\workspace\migration-report.json",
                    "metadata": {
                        "sizeBytes": 4096,
                        "preview": {"text": '{"providers": 18, "mcp": 2, "conflicts": 0}'},
                    },
                },
                {
                    "type": "timeline",
                    "timelineType": "model_change",
                    "fromModel": {"label": "demo-provider/demo-old"},
                    "toModel": {"label": "demo-provider/demo-model"},
                    "status": "completed",
                },
                {
                    "type": "compaction",
                    "trigger": "auto",
                    "compactReason": "context_limit",
                    "phase": "mid_turn",
                    "preCompactTokenCount": 48000,
                    "timelineStatus": "completed",
                },
                {"type": "text", "text": "迁移完成，报告已保存。默认没有覆盖 DSH 已有配置。"},
            ],
            modelID="demo-model",
            providerID="demo-provider",
            mode="yolo",
            tokens={"input": 1800, "output": 220, "reasoning": 0},
            finish="stop",
        )

        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
            (
                "sess_demo_long",
                "Demo: 长会话分页",
                r"D:\Demo\long",
                1790001000000,
                1790005000000,
                "interactive",
                None,
                "demo-long",
            ),
        )
        for index in range(60):
            role = "user" if index % 2 == 0 else "assistant"
            text = (
                f"这是第 {index + 1} 条演示消息。"
                if role == "user"
                else f"这是第 {index + 1} 条助手回复，用于演示分页和流式导出。"
            )
            add_message(
                con,
                "sess_demo_long",
                f"long_m{index}",
                index,
                role,
                1790001000000 + index * 1000,
                [{"type": "text", "text": text}],
                modelID="demo-model" if role == "assistant" else None,
                providerID="demo-provider" if role == "assistant" else None,
                mode="plan" if role == "assistant" else None,
            )

        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
            (
                "sess_demo_subagent",
                "Demo: 子代理",
                r"D:\Demo\workspace",
                1790002000000,
                1790002600000,
                "subagent_child",
                "sess_demo_config",
                "demo-subagent",
            ),
        )
        add_message(
            con,
            "sess_demo_subagent",
            "sub_m0",
            0,
            "user",
            1790002001000,
            [{"type": "text", "text": "只读检查工作区，不要修改任何文件。"}],
        )
        add_message(
            con,
            "sess_demo_subagent",
            "sub_m1",
            1,
            "assistant",
            1790002002000,
            [{"type": "text", "text": "检查完成：没有发现需要修改的配置。"}],
            modelID="demo-model",
            providerID="demo-provider",
            mode="plan",
        )

        con.commit()
    finally:
        con.close()
    print(f"demo database: {output}")


if __name__ == "__main__":
    build(DEFAULT_OUTPUT)
