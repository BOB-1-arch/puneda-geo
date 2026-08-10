"""
命令行测试工具：向本地后端发一个问题，打印 DeepSeek 的真实原始回答。
用法：
    python ask_cli.py "车载冰箱哪个品牌好？"
    python ask_cli.py "车载冰箱哪个品牌好？" --model deepseek-v4-pro
    python ask_cli.py "车载冰箱哪个品牌好？" --thinking
不传问题参数则会提示手动输入。默认模型 deepseek-v4-flash，默认关闭 thinking 模式。
"""
import sys
import json
import requests

BACKEND_URL = "http://localhost:8000/api/ask/deepseek"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    model = "deepseek-v4-flash"
    if "--model" in sys.argv:
        model = sys.argv[sys.argv.index("--model") + 1]
    thinking = "--thinking" in sys.argv

    question = args[0] if args else input("请输入问题：").strip()
    if not question:
        print("问题不能为空")
        return

    try:
        resp = requests.post(
            BACKEND_URL,
            json={"question": question, "model": model, "thinking": thinking},
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"无法连接后端服务：{e}\n请确认已执行 uvicorn main:app --reload --port 8000")
        return

    if resp.status_code != 200:
        print(f"请求失败（状态码 {resp.status_code}）：{resp.text}")
        return

    data = resp.json()
    print(f"\n===== DeepSeek 原始回答（model={data['model']}） =====")
    print(data["raw_answer"])
    if data.get("reasoning_content"):
        print("\n===== 思维链 reasoning_content =====")
        print(data["reasoning_content"])
    print("\n===== 完整原始响应（JSON） =====")
    print(json.dumps(data["raw_response"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
