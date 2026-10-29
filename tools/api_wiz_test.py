# -*- coding: utf-8 -*-
"""端到端验证「云端 API 接入向导」的界面流程。

用真实的 pywebview 窗口跑，桩掉后端接口，验证：
  1. 保存即验证：点「保存」后系统去验证地址 + 密钥
  2. 验证成功后弹模型清单并按档位分组渲染，旗舰排在最前
  3. 厂商不给清单时降级到第三步（手填模型名）
  4. Key 无效时弹窗关闭、表单报错，不进模型选择
  5. 选中模型后能落档，档位徽标出现在模型切换菜单里

用法：python tools/api_wiz_test.py
"""
import json
import os
import threading
import time

import webview

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html").replace("\\", "/")

MOCK_MODELS = [
    {"id": "acme-pro-max", "tier": "flagship", "label": "旗舰", "hint": "", "note": "",
     "price_in": 3.0, "price_out": 15.0, "owned_by": "acme"},
    {"id": "acme-standard", "tier": "balanced", "label": "均衡", "hint": "", "note": "",
     "price_in": 0.5, "price_out": 1.5, "owned_by": "acme"},
    {"id": "acme-flash-lite", "tier": "economy", "label": "轻量", "hint": "", "note": "",
     "price_in": 0.05, "price_out": 0.1, "owned_by": "acme"},
]

TEST_JS = r"""
(function(){
  window.__t = null;
  (async function(){
  const out = {};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const vis = id => {
    const el = document.getElementById(id);
    if(!el) return null;
    return getComputedStyle(el).display !== 'none';
  };

  try{
    /* ---------- 0. 打开设置，加载模型档案表单 ---------- */
    await showSettings();
    await sleep(250);
    out.settingsOpen = document.getElementById('mask').classList.contains('show');

    /* ---------- 1. Key 无效：保存后弹窗关闭、表单报错，不进模型选择 ---------- */
    document.getElementById('s_base').value = 'https://bad.example/v1';
    document.getElementById('s_key').value = 'sk-bad';
    document.getElementById('s_name').value = '坏Key测试';
    saveModel();
    await sleep(500);
    out.badKeyMaskClosed = !document.getElementById('apiMask').classList.contains('show');
    out.badKeyTip = (document.getElementById('saveTip').textContent || '').trim();

    /* ---------- 2. 验证成功 -> 第二步，清单按档位分组 ---------- */
    document.getElementById('s_base').value = 'https://ok.example/v1';
    document.getElementById('s_key').value = 'sk-good';
    document.getElementById('s_name').value = '厂商网关';
    saveModel();
    await sleep(600);
    out.afterOkStep2 = vis('apiWizStep2');
    out.afterOkMaskOpen = document.getElementById('apiMask').classList.contains('show');
    const rows = document.querySelectorAll('#apiModelList .mlist-row');
    out.modelRowCount = rows.length;
    out.groupCount = document.querySelectorAll('#apiModelList .mlist-group').length;
    out.firstRowId = rows.length ? rows[0].getAttribute('data-id') : '';
    out.firstRowHasFlagshipBadge = rows.length
      ? !!rows[0].querySelector('.tier-flagship') : false;
    out.firstGroupLabel = (document.querySelector('#apiModelList .mlist-group')||{}).textContent || '';
    out.selectedCount = document.querySelectorAll('#apiModelList .mlist-row.on').length;
    out.priceShown = (document.querySelector('#apiModelList .wiz-price')||{}).textContent || '';

    /* ---------- 3. 选模型并落档 ---------- */
    const economyRow = Array.from(rows).find(r => r.getAttribute('data-id') === 'acme-flash-lite');
    if(economyRow) apiPickModel(economyRow);
    await sleep(60);
    out.pickedTier = (document.querySelector('#apiModelList .mlist-row.on .tier-badge')||{}).textContent || '';
    await apiUseModel();
    await sleep(400);
    out.maskClosedAfterSave = !document.getElementById('apiMask').classList.contains('show');
    out.savedProfiles = (window._profiles || []).map(p => ({name:p.name, model:p.model, tier:p.tier}));
    out.savedTip = (document.getElementById('saveTip').textContent || '').trim();

    /* ---------- 4. 厂商不给清单 -> 第三步手填模型名 ---------- */
    document.getElementById('s_base').value = 'https://gw.example/v1';
    document.getElementById('s_key').value = 'sk-good';
    document.getElementById('s_name').value = '本地推理 · lightweight';
    saveModel();
    await sleep(600);
    out.manualStep3 = vis('apiWizStep3');
    out.manualMaskOpen = document.getElementById('apiMask').classList.contains('show');
    out.manualHint = (document.querySelector('#apiWizStep3 .manual-hint')||{}).textContent || '';

    /* ---------- 6. 手填模型名后验证通过并落档 ---------- */
    document.getElementById('api_manual_model').value = 'qwen2.5-7b-instruct';
    apiManualCheck();
    await sleep(600);
    out.manualMaskClosed = !document.getElementById('apiMask').classList.contains('show');
    out.manualSavedProfiles = (window._profiles || []).map(p => ({name:p.name, model:p.model, tier:p.tier}));

    /* ---------- 7. 档位徽标出现在模型切换菜单 ---------- */
    await pywebview.api.set_profiles([
      {name:'厂商旗舰', llm_mode:'cloud', model:'acme-pro-max', api_key:'k', base_url:'https://x/v1', tier:'flagship'},
      {name:'本地推理', llm_mode:'cloud', model:'qwen2.5-7b-instruct', api_key:'k', base_url:'https://y/v1', tier:'economy'}
    ]);
    await renderModelMenu();
    await sleep(150);
    const menuRows = document.querySelectorAll('#modelList .pop-item');
    out.menuItemCount = menuRows.length;
    out.menuHasFlagship = !!document.querySelector('#modelList .tier-flagship');
    out.menuHasEconomy = !!document.querySelector('#modelList .tier-economy');

    /* ---------- 8. 输入框模型名带档位徽标 ---------- */
    refreshModelName({llm_mode:'cloud', model:'acme-pro-max', configured:true});
    await sleep(80);
    const ms = document.getElementById('msName');
    out.msNameText = ms ? ms.textContent.trim() : '';
    out.msHasBadge = !!(ms && ms.querySelector('.tier-badge'));

  } catch (e) {
    out.error = String(e) + ' @ ' + ((e.stack||'').split('\n')[1] || '');
  }
  window.__t = out;
  })();
  return 1;
})()
"""


class Api:
    """桩后端：按 base_url 区分场景（ok / manual / badkey），记录落档调用。"""

    def __init__(self):
        self.profiles = []
        self.last = None

    def get_settings(self):
        return {"theme": "dark", "configured": True, "llm_mode": "cloud",
                "glass_enabled": True, "glass_refract": True,
                "workbench_enabled": True, "profiles": list(self.profiles),
                "base_url": "", "model": "", "api_key": "", "llm_base_url": "",
                "llm_model": ""}

    @staticmethod
    def _mode(base_url):
        b = (base_url or "").lower()
        if "gw.example" in b:
            return "manual"
        if "bad.example" in b:
            return "badkey"
        return "ok"

    def api_probe(self, base_url, api_key):
        mode = self._mode(base_url)
        if mode == "ok":
            return {"ok": True, "key_ok": True, "models": MOCK_MODELS,
                    "needs_manual": False, "message": "Key 可用，该接口共提供 3 个模型",
                    "base_url": base_url}
        if mode == "manual":
            return {"ok": False, "key_ok": False, "models": [], "needs_manual": True,
                    "message": "该接口不提供模型清单。请手动填写模型名，稍后用真实请求验证。",
                    "base_url": base_url}
        return {"ok": False, "key_ok": False, "models": [], "needs_manual": False,
                    "message": "API Key 无效或没有权限（HTTP 401）", "base_url": base_url}

    def api_probe_model(self, base_url, api_key, model):
        if model != "qwen2.5-7b-instruct":
            return {"ok": False, "message": "模型 ID 不被该端点支持（HTTP 404）"}
        return {"ok": True, "message": "验证通过：%s 可以正常调用" % model}

    def api_add_profile(self, base_url, api_key, model, name=None, tier=None):
        self.last = {"base_url": base_url, "model": model, "name": name, "tier": tier}
        prof = {"name": name or (model + " @ host"), "llm_mode": "cloud",
                "model": model, "base_url": base_url, "api_key": api_key,
                "tier": tier or "balanced"}
        self.profiles = self.profiles + [prof]
        return {"ok": True, "profile": prof,
                "settings": self.get_settings()}

    def get_status(self):
        return {"engine": "cloud", "llm": True, "browser": False,
                "browser_text": "未打开", "tools": "", "ctx": 40960}

    def set_profiles(self, profiles):
        """测试用：直接设定档案列表，验证菜单渲染。"""
        self.profiles = list(profiles or [])
        return {"ok": True}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {}
        return _noop


def main():
    api = Api()
    webview.create_window("apiwiztest", UI, js_api=api, width=1100, height=800)

    def _run():
        for _ in range(100):
            if webview.windows:
                break
            time.sleep(0.1)
        time.sleep(2.5)
        txt = "{}"
        try:
            win = webview.windows[0]
            # 把后端桩暴露给页面（页面里的 API 调用要转发到 Python 侧）
            win.evaluate_js("window.__probeMode='ok';")
            win.evaluate_js(TEST_JS)
            for _ in range(80):
                r = win.evaluate_js("window.__t ? JSON.stringify(window.__t) : null")
                if r:
                    txt = r
                    break
                time.sleep(0.2)
            else:
                txt = "超时：未取到结果"
        except Exception as e:
            txt = "EVAL 失败: %r" % (e,)
        try:
            os.makedirs(os.path.join(ROOT, "shots"), exist_ok=True)
            with open(os.path.join(ROOT, "shots", "api_wiz_test.json"), "w",
                      encoding="utf-8") as f:
                f.write(txt)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_run, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
