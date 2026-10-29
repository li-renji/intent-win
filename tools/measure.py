#!/usr/bin/env python3
"""测量截图 BMP 的平均 RGB，并按区域（主体/顶部亮边/左侧栏）分别统计。
用法: python tools/measure.py <file.bmp>"""
import sys, struct

def load(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:2] == b"BM", "not BMP"
    off = struct.unpack_from("<I", data, 10)[0]
    w, h = struct.unpack_from("<ii", data, 18)[0:2]
    h = abs(h)  # 顶置 BMP 的 biHeight 为负：取绝对值，按文件顺序即自上而下
    # 24-bit: 每行按 4 字节对齐
    row_bytes = (w * 3 + 3) // 4 * 4
    px = bytearray()
    for y in range(h):
        base = off + y * row_bytes
        row = data[base:base + w * 3]
        px.extend(row)
    return w, h, px

def avg(px, w, h, x0, y0, x1, y1):
    r = g = b = n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = (y * w + x) * 3
            # 24-bit BMP 此处按 R,G,B 存储（截图脚本已做 BGR->RGB）
            r += px[i]; g += px[i+1]; b += px[i+2]
            n += 1
    if n == 0:
        return (0, 0, 0)
    return (round(r/n), round(g/n), round(b/n))

def main():
    path = sys.argv[1]
    w, h, px = load(path)
    print("尺寸 %dx%d" % (w, h))
    # 主体区域（避开最外 4px 亮边 + 顶栏）
    mx0, my0, mx1, my1 = int(w*0.06), int(h*0.10), int(w*0.94), int(h*0.94)
    print("主体平均 RGB:", avg(px, w, h, mx0, my0, mx1, my1))
    # 顶部亮边（y 0..3）
    print("顶部亮边 RGB:", avg(px, w, h, int(w*0.1), 0, int(w*0.9), 3))
    # 左亮边（x 0..3）
    print("左侧亮边 RGB:", avg(px, w, h, 0, int(h*0.1), 3, int(h*0.9)))
    # 右上角内部（看是否为黑玻璃而非暖色）
    print("右上内区 RGB:", avg(px, w, h, int(w*0.75), int(h*0.15), int(w*0.95), int(h*0.4)))

if __name__ == "__main__":
    main()
