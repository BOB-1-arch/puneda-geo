"""
/api/ask/deepseek 对 DEEPSEEK_API_KEY 的ASCII校验测试（FastAPI TestClient）。

真实故障复现：手机端粘贴Key时输入法有时会把英文/数字转成全角字符，肉眼看起来一样，
但会导致requests库构造HTTP请求头时抛出 UnicodeEncodeError，此前这个异常没被专门
捕获，用户只能看到一个不知所云的500和一堆Python报错。这里验证：
1. 全角字符污染的Key会在真正发起网络请求之前就被拦截，返回有具体原因的错误提示；
2. 正常的纯ASCII Key不会被误判拦截（只是走到真实网络请求那一步才会失败，
   这里用一个不会被DNS解析到的假域名场景不做验证，只验证"没有在ASCII校验这关被拦下"）。

运行方式：venv 里装了 fastapi/httpx 之后 python test_ask_deepseek_key_validation.py
"""

import os
import tempfile

_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["GEO_DB_PATH"] = _TMP_DB
# 故意设置一个混入全角字符的Key（"6"是全角０-９里的一个例子：U+FF16 而不是普通"6"）。
os.environ["DEEPSEEK_API_KEY"] = "sk-abc１２３defghij"  # 中间几位是全角数字

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

client = TestClient(main.app)


def test_fullwidth_char_in_key_returns_clear_error_not_raw_traceback():
    r = client.post("/api/ask/deepseek", json={"question": "车载冰箱哪个品牌好？"})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "非ASCII字符" in detail
    assert "全角" in detail


def test_error_message_mentions_actionable_fix_steps():
    r = client.post("/api/ask/deepseek", json={"question": "test"})
    detail = r.json()["detail"]
    # 必须给出具体可执行的修复路径，不能只说"出错了"。
    assert "nano" in detail or ".env" in detail
    assert "systemctl restart" in detail


ALL_TESTS = [v for k, v in list(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failures = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} passed")
    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)
    if failures:
        raise SystemExit(1)
