# -*- coding: utf-8 -*-
"""电脑文件桥（agent/pc_bridge.py）端到端测试。

覆盖 Node 版全部 8 个接口 + 安全模型：
  1. /api/ping 免 token
  2. 无 token / 错 token → 401
  3. /api/list 盘符 + 目录
  4. /api/read 文本 + 二进制 base64 + 目录报错
  5. /api/write 写文本（手机改文件保存回电脑的核心能力）
  6. /api/mkdir + /api/delete
  7. /api/download 二进制下载
  8. /api/upload 二进制上传
  9. --root 限制：越界路径拒绝
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.pc_bridge import PCBridge

PORT = 18999
results = []


def check(name, ok, extra=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" | " + extra) if extra else ""))
    results.append(ok)


def req(method, path, body=None, token=None, raw=False, port=None):
    from urllib.parse import quote
    port = port or PORT
    # 路径部分按手机 WebView 行为做 percent 编码（中文文件名）
    if "?" in path:
        base, _, qs_s = path.partition("?")
        path = quote(base, safe="/") + "?" + quote(qs_s, safe="=&")
    else:
        path = quote(path, safe="/")
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        if raw:
            data = body if isinstance(body, bytes) else body.encode()
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            payload = resp.read()
            if raw or not payload:
                return resp.status, ({"raw": payload} if not raw else payload)
            try:
                return resp.status, json.loads(payload.decode("utf-8"))
            except Exception:
                return resp.status, {"raw": payload[:200]}
    except urllib.error.HTTPError as e:
        payload = e.read()
        if not payload:
            return e.code, {}
        try:
            return e.code, json.loads(payload.decode("utf-8"))
        except Exception:
            return e.code, {"raw": payload[:100]}


def main():
    tmp = tempfile.mkdtemp(prefix="qd_bridge_")
    try:
        # 造测试数据
        txt = os.path.join(tmp, "hello.txt")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("第一行\nsecond line")
        binp = os.path.join(tmp, "pic.bin")
        with open(binp, "wb") as f:
            f.write(bytes(range(256)) * 4)

        # 场景 A：无 root 限制
        bridge = PCBridge(port=PORT, root=None, token_file=None)
        bridge.start()
        tok = bridge.token
        time.sleep(0.3)

        # 1. ping 免 token
        code, d = req("GET", "/api/ping")
        check("1. ping 免 token 连通", code == 200 and d.get("ok"))

        # 2. token 校验
        code, d = req("GET", "/api/list?path=" + tmp)
        check("2a. 无 token → 401", code == 401)
        code, d = req("GET", "/api/list?path=" + tmp, token="wrong-token")
        check("2b. 错 token → 401", code == 401)
        code, d = req("GET", "/api/list?path=" + tmp, token=tok)
        check("2c. 正确 token → 200", code == 200)

        # 3. list 盘符 + 目录
        code, d = req("GET", "/api/list", token=tok)
        drives = [i["name"] for i in d.get("items", [])]
        check("3a. 空 path 列盘符", code == 200 and any(c.endswith(":\\") for c in drives),
              str(drives[:3]))
        code, d = req("GET", "/api/list?path=" + tmp, token=tok)
        names = sorted(i["name"] for i in d.get("items", []))
        check("3b. 列目录内容", names == ["hello.txt", "pic.bin"], str(names))

        # 4. read 文本 / 二进制 / 目录
        code, d = req("GET", "/api/read?path=" + txt, token=tok)
        check("4a. read 文本", code == 200 and d.get("text") and "second line" in d["content"])
        code, d = req("GET", "/api/read?path=" + binp, token=tok)
        import base64
        ok_bin = code == 200 and not d.get("text") and \
            base64.b64decode(d["content"]) == bytes(range(256)) * 4
        check("4b. read 二进制 base64", ok_bin)
        code, d = req("GET", "/api/read?path=" + tmp, token=tok)
        check("4c. read 目录 → 400", code == 400)

        # 5. write（核心：手机改文件写回电脑）
        newfile = os.path.join(tmp, "sub", "手机改的.txt")
        code, d = req("POST", "/api/write", token=tok,
                      body={"path": newfile, "content": "手机写入的中文内容\n第二行"})
        ok_w = code == 200 and os.path.exists(newfile) and \
            open(newfile, encoding="utf-8").read() == "手机写入的中文内容\n第二行"
        check("5. write 写文本（含自动建目录）", ok_w)
        code, d = req("POST", "/api/write", token=tok,
                      body={"path": txt, "content": "覆盖后的内容"})
        check("5b. write 覆盖已有文件",
              code == 200 and open(txt, encoding="utf-8").read() == "覆盖后的内容")

        # 6. mkdir + delete
        nd = os.path.join(tmp, "新建目录")
        code, d = req("POST", "/api/mkdir", token=tok, body={"path": nd})
        check("6a. mkdir 递归新建", code == 200 and os.path.isdir(nd))
        code, d = req("POST", "/api/delete", token=tok, body={"path": newfile})
        check("6b. delete 文件", code == 200 and not os.path.exists(newfile))

        # 7. download
        code, payload = req("GET", "/api/download?path=" + binp, token=tok, raw=True)
        check("7. download 二进制", code == 200 and payload == bytes(range(256)) * 4)

        # 8. upload
        up = os.path.join(tmp, "up", "上传的照片.jpg")
        code, d = req("POST", "/api/upload?path=" + up, token=tok,
                      body=b"\xff\xd8\xff\xe0binarydata", raw=True)
        check("8. upload 二进制",
              code == 200 and open(up, "rb").read() == b"\xff\xd8\xff\xe0binarydata")

        # 9. CORS 预检
        code, _ = req("OPTIONS", "/api/list")
        check("9. OPTIONS 预检 204", code == 204)

        bridge.stop()

        # 场景 B：root 限制
        PORT_B = PORT + 1
        bridge2 = PCBridge(port=PORT_B, root=tmp, token_file=None)
        bridge2.start()
        time.sleep(0.3)
        tok2 = bridge2.token
        inside = os.path.join(tmp, "root_ok.txt")
        code, d = req("POST", "/api/write", token=tok2, port=PORT_B,
                      body={"path": inside, "content": "root 内"})
        check("10a. root 内可写", code == 200 and os.path.exists(inside))
        # 越界（系统盘）
        outside = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "越界.txt")
        code, d = req("POST", "/api/write", token=tok2, port=PORT_B,
                      body={"path": outside, "content": "越界"})
        check("10b. root 外拒绝", code == 500 and "超出允许访问的根目录" in d.get("error", ""))
        bridge2.stop()

        print("\n结果: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
