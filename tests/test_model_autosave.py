# -*- coding: utf-8 -*-
"""模型档案存储测试：后端忠实存储前端传来的 profiles 列表并应用活动配置。

说明：去重（同一 API 只存一条）由前端按名称 upsert 保证，后端只负责
把 save_settings 收到的 profiles 列表与活动配置原样落盘 / 回读。

验证：
1. 保存 profiles 列表 → 落盘正确，get_settings 回读一致
2. 同一名称更新保存 → 仍为 1 条（前端 upsert，后端不擅自新增）
3. 保存空 profiles → 清空
4. 应用活动配置 → base_url/key/model 写入 配置.qdai 且回读一致
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.app import Api                                   # noqa: E402
from agent.config import load_config                        # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s %s" % ("[PASS]" if cond else "[FAIL]", name, detail))


def _new_project():
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "配置.qdai"), "w", encoding="utf-8") as f:
        f.write("llm_mode=cloud\nbase_url=\napi_key=\nmodel=\n")
    return tmp


def _read_profiles_file(project):
    cfg = load_config(os.path.join(project, "配置.qdai"))
    raw = cfg.get("profiles", "[]")
    return json.loads(raw) if isinstance(raw, str) else (raw or [])


class _Stub:
    """复用 Api 的 save_settings / get_settings，不构造整个桌面应用。"""
    get_settings = Api.get_settings
    save_settings = Api.save_settings
    _load_profiles = Api._load_profiles


def _stub(project):
    stub = _Stub()
    stub.cfg = load_config(os.path.join(project, "配置.qdai"))
    return stub


def run():
    # ---- 1. 保存 profiles 列表 → 落盘 + 回读一致 ----
    proj = _new_project()
    stub = _stub(proj)
    profs = [{"name": "MiMo 主力", "llm_mode": "cloud",
              "base_url": "https://api.xiaomimimo.com/v1",
              "model": "mimo-v2.6-pro", "api_key": "sk-test123",
              "llm_base_url": "", "llm_model": ""}]
    Api.save_settings(stub, {"profiles": profs,
                            "base_url": profs[0]["base_url"],
                            "model": profs[0]["model"],
                            "api_key": profs[0]["api_key"]})
    saved = _read_profiles_file(proj)
    check("落盘：profiles 写入 1 条", len(saved) == 1 and saved[0]["name"] == "MiMo 主力")
    gs = Api.get_settings(stub)
    check("回读：get_settings 返回该档案", len(gs["profiles"]) == 1 and
          gs["profiles"][0]["model"] == "mimo-v2.6-pro")
    check("回读：configured 为真", gs["configured"] is True)

    # ---- 2. 同一名称更新保存 → 仍为 1 条 ----
    profs[0]["api_key"] = "sk-updated456"
    Api.save_settings(stub, {"profiles": profs,
                            "api_key": "sk-updated456"})
    saved2 = _read_profiles_file(proj)
    check("去重：更新同名档案仍 1 条", len(saved2) == 1 and
          saved2[0]["api_key"] == "sk-updated456")

    # ---- 3. 新增第二条不同名称 ----
    profs.append({"name": "公司网关", "llm_mode": "cloud",
                  "base_url": "https://gw.corp.com/v1",
                  "model": "gpt-4o", "api_key": "sk-corp",
                  "llm_base_url": "", "llm_model": ""})
    Api.save_settings(stub, {"profiles": profs})
    check("新增：落盘两条", len(_read_profiles_file(proj)) == 2)

    # ---- 4. 保存空 profiles → 清空 ----
    Api.save_settings(stub, {"profiles": []})
    check("清空：空列表落盘为空", _read_profiles_file(proj) == [])

    # ---- 5. 应用活动配置（本地模式） ----
    Api.save_settings(stub, {"llm_mode": "local",
                             "llm_base_url": "http://127.0.0.1:8080/v1",
                             "llm_model": "qwen3-14b"})
    cfg = load_config(os.path.join(proj, "配置.qdai"))
    check("应用：本地端点写入", cfg["llm_base_url"] == "http://127.0.0.1:8080/v1")
    check("应用：本地模型写入", cfg["llm_model"] == "qwen3-14b")
    check("应用：local 模式未配置云端 Key → configured 视情况",
          Api.get_settings(stub)["llm_mode"] == "local")

    shutil.rmtree(proj, ignore_errors=True)
    ok_all = all(r[1] for r in RESULTS)
    print("\n==== test_model_autosave: %d/%d 通过 ====" % (
        sum(1 for r in RESULTS if r[1]), len(RESULTS)))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(run())
