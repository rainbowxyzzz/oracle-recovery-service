"""Real Harness runtime + local synthetic model, no external model/data service."""
import importlib.util
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

requests = []
expected = {"pipeline_id":"route-test", "sm4_task_id":"task-test", "file_name":"test.dmp", "reply":"请确认"}


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.end_headers()
            chunks=[{"delta":{"role":"assistant","content":json.dumps(expected,ensure_ascii=False)},"finish_reason":None},
                    {"delta":{},"finish_reason":"stop"}]
            for c in chunks:
                value={"id":"test", "object":"chat.completion.chunk", "model":"deepseek-v4-flash", "created":1,
                       "choices":[{"index":0,**c}], "usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}
                self.wfile.write(("data: "+json.dumps(value)+"\n\n").encode());self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(200);self.send_header("Content-Type","application/json");self.end_headers()
            self.wfile.write(json.dumps({"id":"test","choices":[{"index":0,"message":{"role":"assistant","content":json.dumps(expected)},"finish_reason":"stop"}]}).encode())


if __name__ == "__main__":
    server=ThreadingHTTPServer(("127.0.0.1",0),Model)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    os.environ["HARNESS_MODEL"]="deepseek-v4-flash"
    os.environ["DEEPSEEK_BASE_URL"]=f"http://127.0.0.1:{server.server_port}/v1"
    os.environ["DEEPSEEK_API_KEY"]="synthetic-test-key"
    spec=importlib.util.spec_from_file_location("planner",Path(__file__).with_name("app.py"))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    try:
        result=module.run_harness({"message":"把test.dmp更新DWD", "pipelines":[{"id":"route-test"}],"sm4_tasks":[{"id":"task-test"}]})
        assert result==expected,result
        assert requests,"Runtime did not contact the synthetic model"
        assert all(not r.get("tools") for r in requests),"Planner unexpectedly exposes tools"
        print("PASS: real Harness 0.1.1rc1 boot, local synthetic model, JSON result, no model-facing tools")
    finally:
        server.shutdown()
