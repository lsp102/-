"""tests — LLM 驱动 Agent 的回归用例

覆盖三件事：
  1. advisor 确实声明成 llm 驱动，且带 prompt
  2. 没有 key 时自动退回 mock，场景结论不受影响
  3. 有 key + real 模式时，请求真的发出去、响应真的被解析

第 3 条用本地假端点验证协议层，不需要真 key。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.pack import load_pack       # noqa: E402
from harness.runner import run_scenario  # noqa: E402

PACK = ROOT / "packs" / "fall-response"
ENV_KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE", "HARNESS_MODE")


class LlmAgentTest(unittest.TestCase):
    def setUp(self):
        self.pack = load_pack(PACK)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_advisor_declared_as_llm(self):
        ad = self.pack.agents["advisor"]
        self.assertEqual(ad["driver"], "llm")
        self.assertTrue(str(ad.get("prompt") or "").strip())

    def test_mock_mode_without_key(self):
        res, _ = run_scenario(self.pack, self.pack.scenario("s3"), mode="mock")
        self.assertEqual(res.final, "answered")
        text = (res.ctx.get("advice") or {}).get("text", "")
        self.assertTrue(text.startswith("[mock:"), text)

    def test_real_mode_calls_endpoint(self):
        seen: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                seen["auth"] = self.headers.get("Authorization")
                seen["user"] = body["messages"][-1]["content"]
                payload = json.dumps(
                    {"choices": [{"message": {"content": "【模拟端点】请先别动老人"}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            os.environ["DEEPSEEK_API_KEY"] = "test-key"
            os.environ["DEEPSEEK_BASE"] = f"http://127.0.0.1:{port}"
            res, _ = run_scenario(self.pack, self.pack.scenario("s3"), mode="real")
        finally:
            srv.shutdown()
            srv.server_close()

        self.assertEqual((res.ctx.get("advice") or {}).get("text"), "【模拟端点】请先别动老人")
        self.assertEqual(seen.get("auth"), "Bearer test-key")
        # 输入必须是结构化结论，而不是原始提问——模型只做措辞，不做判断
        self.assertIn("风险等级", seen.get("user", ""))


if __name__ == "__main__":
    unittest.main()
