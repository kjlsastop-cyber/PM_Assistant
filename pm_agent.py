# -*- coding: utf-8 -*-
"""产品经理助手 Agent（本地命令行版）

使用 OpenAI 兼容接口，可对接 OpenAI / DeepSeek / 通义 / 本地 Ollama 等。
配置见 .env（参考 .env.example）。
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).parent


def load_system_prompt() -> str:
    prompts_dir = BASE_DIR / "prompts"
    files = sorted(prompts_dir.glob("*.md")) if prompts_dir.exists() else []
    if not files:
        raise SystemExit("prompts/ 目录下没有助手提示词文件（.md）。")
    return files[0].read_text(encoding="utf-8").strip()


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写 API Key。"
        )

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
    )
    model = os.getenv("MODEL_NAME", "gpt-4o-mini").strip()
    system_prompt = load_system_prompt()

    messages = [{"role": "system", "content": system_prompt}]

    print(f"产品经理助手已启动（模型：{model}）")
    print("命令：exit 退出 | clear 清空对话")
    print("快捷指令示例：@解决方案 政数局统一认证平台 预算200万\n")

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("再见！")
            break
        if user_input.lower() == "clear":
            messages = [{"role": "system", "content": system_prompt}]
            print("（对话已清空）\n")
            continue

        messages.append({"role": "user", "content": user_input})

        print("助手 > ", end="", flush=True)
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
            )
            reply_parts = []
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    print(delta, end="", flush=True)
                    reply_parts.append(delta)
            print("\n")
            messages.append({"role": "assistant", "content": "".join(reply_parts)})
        except Exception as e:  # 网络/鉴权/接口错误，不中断会话
            print(f"\n[调用失败] {e}\n")
            messages.pop()  # 移除本轮失败的用户消息


if __name__ == "__main__":
    main()
