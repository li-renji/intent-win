# -*- coding: utf-8 -*-
"""本地模型管理器（对等于手机端 ModelCatalog.kt）。

目录约定（与手机端 files/models/<id>/ 保持一致，便于两边互认）：
    models/                     模型根目录
    models/<modelId>/           每个模型独立目录
    models/.active              当前激活模型 id（纯文本）

职责：扫描已下载模型、按可用内存推荐、激活/切换、元数据查询。
**不做下载**：下载涉及 GB 级流量与许可证接受，由用户显式触发（见 README）。

安全：删除模型是不可逆操作（C6），必须显式传 confirm=True，
否则一律拒绝并返回原因——不允许 UI 一个按钮就删掉几个 GB 的模型。
"""
import json
import os
import shutil
import sys

GGUF = ".gguf"
ACTIVE_FILE = ".active"

# GGUF 常见量化档位的经验命名（用于估算显存/内存占用，仅机器型号友好名，非硬约束）
_QUANT_HINTS = (
    ("q2_k", 0.32), ("q3_k", 0.38), ("q4_0", 0.44), ("q4_k", 0.48),
    ("q5_0", 0.54), ("q5_k", 0.56), ("q6_k", 0.65), ("q8_0", 0.85),
    ("f16", 2.0), ("f32", 4.0),
)


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "%.0f%s" % (n, unit) if unit == "B" else "%.2f%s" % (n, unit)
        n /= 1024.0
    return "%.2fPB" % n


def estimate_ram_gb(path, params_b=None):
    """粗略估算加载该文件所需内存：优先看文件体积+量化档位，其次按参数量。

    返回 (GB 估算, 估算依据)，低于 0 表示无法估算。
    """
    name = os.path.basename(path).lower()
    try:
        size_gb = os.path.getsize(path) / 1024 ** 3
    except OSError:
        size_gb = 0.0
    quant = next((h for h, _ in _QUANT_HINTS if h in name), None)
    if quant:
        # 量化文件体积 ≈ 权重占用；运行时再加约 1.2GB 的 KV cache / 计算缓冲
        return round(size_gb * 1.15 + 1.2, 2), "按文件体积(%s, %s)" % (_fmt_size(size_gb * 1024 ** 3), quant)
    if size_gb:
        return round(size_gb * 1.15 + 1.2, 2), "按文件体积粗估"
    if params_b:
        return round(params_b * 0.6 * 1.15 + 1.2, 2), "按参数量 %sB 粗估" % params_b
    return -1.0, "信息不足"


class ModelManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.get("project_root", ".")
        self.models_dir = os.path.join(self.root,
                                       cfg.get("local_models_dir", "models"))
        os.makedirs(self.models_dir, exist_ok=True)
        self._active_path = os.path.join(self.models_dir, ACTIVE_FILE)

    # ---------- 扫描 ----------
    def scan(self):
        """扫描模型根目录，返回模型信息列表（每项含 id/path/size/ram_gb）。"""
        items = []
        if not os.path.isdir(self.models_dir):
            return items
        for entry in sorted(os.listdir(self.models_dir)):
            if entry.startswith("."):
                continue
            d = os.path.join(self.models_dir, entry)
            if os.path.isfile(d) and d.lower().endswith(GGUF):
                files = [d]
                mid = os.path.splitext(entry)[0]
            elif os.path.isdir(d):
                ggufs = [os.path.join(dp, f)
                         for dp, _, fs in os.walk(d) for f in fs
                         if f.lower().endswith(GGUF)]
                if not ggufs:
                    continue
                files = sorted(ggufs)
                mid = entry
            else:
                continue
            total = sum(os.path.getsize(f) for f in files if os.path.exists(f))
            main = files[0]
            ram, basis = estimate_ram_gb(main)
            items.append({
                "id": mid,
                "files": [os.path.relpath(f, self.models_dir) for f in files],
                "size": total,
                "size_human": _fmt_size(total),
                "ram_gb": ram,
                "ram_basis": basis,
                "path": d,
                "active": mid == self.active_id(),
            })
            # 主模型文件路径（引擎加载用）
            items[-1]["load_path"] = files[0]
        return items

    # ---------- 激活 ----------
    def active_id(self):
        try:
            with open(self._active_path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def set_active(self, model_id):
        """写入 .active 标记（与手机端同名文件语义一致）。"""
        hit = next((m for m in self.scan() if m["id"] == model_id), None)
        if not hit:
            return False, "模型不存在: %s" % model_id
        with open(self._active_path, "w", encoding="utf-8") as f:
            f.write(model_id)
        return True, hit["load_path"]

    def resolve(self, model_id_or_path=None):
        """把「模型 id 或路径」解析成可加载的绝对 GGUF 路径。"""
        raw = (model_id_or_path or "").strip()
        if not raw:
            raw = self.active_id()
        if not raw:
            items = self.scan()
            raw = items[0]["id"] if items else ""
            if not raw:
                return None
        cand = raw if os.path.isabs(raw) else os.path.join(self.models_dir, raw)
        if os.path.isfile(cand):
            return cand
        if os.path.isdir(cand):
            for dp, _, fs in os.walk(cand):
                for f in sorted(fs):
                    if f.lower().endswith(GGUF):
                        return os.path.join(dp, f)
        # 可能给的是 id，且模型在子目录里
        for m in self.scan():
            if m["id"] == raw:
                return m["load_path"]
        return None

    # ---------- 推荐 ----------
    def recommend(self, ram_total_gb=0.0):
        """按可用内存推荐一个模型：内存够得下的最大模型。

        ram_total_gb=0 时按「体积最小」推荐（弱机兜底）。
        返回 (model_dict | None, 说明)。
        """
        items = self.scan()
        if not items:
            return None, "models/ 目录为空，还没有本地模型"
        safe = [m for m in items if 0 <= m["ram_gb"] > 0]
        if not safe:
            return items[0], "无法估算内存占用，给出体积最小的模型"
        if ram_total_gb <= 0:
            best = min(safe, key=lambda m: m["ram_gb"])
            return best, "未指定内存，推荐占用最小的模型"
        budget = ram_total_gb * 0.72  # 留 28% 给系统与其它程序
        fit = [m for m in safe if m["ram_gb"] <= budget]
        if not fit:
            best = min(safe, key=lambda m: m["ram_gb"])
            return best, ("无模型能放进 %.1fGB 预算，退而推荐最小的 %s（需 %.2fGB）"
                          % (budget, best["id"], best["ram_gb"]))
        best = max(fit, key=lambda m: m["ram_gb"])
        return best, "在 %.1fGB 预算内推荐：%s（约需 %.2fGB）" % (budget, best["id"], best["ram_gb"])

    # ---------- 删除（危险操作，强制二次确认 C6） ----------
    def delete(self, model_id, confirm=False):
        """删除模型。confirm 必须为 True，否则拒绝执行。"""
        if not confirm:
            return False, ("删除模型不可恢复，请在调用时显式传 confirm=True"
                           "（UI 侧应弹出二次确认并要求用户输入模型 id 复核）")
        hit = next((m for m in self.scan() if m["id"] == model_id), None)
        if not hit:
            return False, "模型不存在: %s" % model_id
        target = os.path.join(self.models_dir, model_id)
        try:
            if os.path.isfile(target):
                os.unlink(target)
            else:
                shutil.rmtree(target)
        except OSError as e:
            return False, "删除失败: %s" % e
        if self.active_id() == model_id:
            try:
                os.unlink(self._active_path)
            except OSError:
                pass
        return True, "已删除 %s" % model_id

    # ---------- 摘要 ----------
    def summary(self):
        items = self.scan()
        return {
            "models_dir": self.models_dir,
            "count": len(items),
            "total_size": sum(m["size"] for m in items),
            "total_human": _fmt_size(sum(m["size"] for m in items)),
            "active": self.active_id(),
            "models": items,
        }


def main():
    """CLI: python -m agent.model_mgr [scan|summary|recommend <ram_gb>]"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = {"project_root": root, "local_models_dir": "models"}
    mgr = ModelManager(cfg)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "scan":
        for m in mgr.scan():
            print("%-28s %8s  需≈%sGB  %s" % (m["id"], m["size_human"], m["ram_gb"],
                                             "*" if m["active"] else ""))
    elif cmd == "recommend":
        ram = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
        m, why = mgr.recommend(ram)
        print(why)
        if m:
            print("→ %s（%s）" % (m["id"], m["size_human"]))
    else:
        s = mgr.summary()
        print(json.dumps(s, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
