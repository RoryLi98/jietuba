# -*- coding: utf-8 -*-
"""滚动方向反转与「重复拼接」的回归测试。

落位由 Python 侧的行签名对齐算出（_StitchWorker._align），偏移是算出来的
而不是让拼接算法猜出来的，所以：

* 往回滚过已拼区域 → 整幅新帧落在画布内 → 不动画布、不计数、不报错；
* 越过画布顶/底 → 按算出的偏移向上补行 / 向下延展，方向随之翻转；
* 结果永远是自然朝向（没有"翻转态画布 + 收尾再翻回来"的约定）。

历史背景：旧实现的正向语义是「新帧接在已拼结果下方」，方向锁在第 2 帧由
Rust 自动检测定下后就不再变，于是往回滚时画布会被裁短、越过顶部后一路
「未找到可靠的重叠区域」；而 Rust 的 LCS 选出的错位候选会让画布**变长**却
重复一段已有内容——那才是用户看到的「重复拼接」，只看高度拦不住它。

行数据说明：每行用 (R, G) 唯一编码内容坐标 y，保证任意两行都不相似——
这样候选偏移的对错可以由「行是否对得上」直接判定。
"""
import threading

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app

W, V, STEP = 120, 100, 20


def row_rgb(y):
    """内容行 y 的颜色：由 y 确定性散列得到。

    行与行之间必须「看起来完全不同」：逐行复核的容差是分段均值偏差 ≤12，
    若相邻行只差几个色阶，错位一个像素的候选也能整段通过——那正是要拦住的
    东西（Rust 的 LCS 就是这么选错的）。真实页面的文字行差异远大于该容差。
    """
    h = (y * 2654435761) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822519) & 0xFFFFFFFF
    h ^= h >> 13
    return (h >> 16) & 0xFF, (h >> 8) & 0xFF, 128


def make_frame(y0, h=V):
    """视口内容 [y0, y0+h)，BGRA32（worker 按 BGRA 解）。"""
    buf = bytearray()
    for i in range(h):
        r, g, b = row_rgb(y0 + i)
        buf += bytes((b, g, r, 255)) * W
    return QImage(bytes(buf), W, h, W * 4, QImage.Format.Format_ARGB32).copy()


def _new_worker():
    from stitch.scroll_window import _StitchWorker

    return _StitchWorker(
        scroll_direction="vertical",
        locked_direction=None,
        duplicate_threshold=0.95,
        emit_fn=lambda payload: None,
    )


def _submit(worker, y0):
    done = threading.Event()
    out = []
    worker._emit = lambda payload: (out.append(payload), done.set())
    worker.submit(make_frame(y0), "vertical")
    assert done.wait(20.0), f"y0={y0} 未在超时内回包"
    return out[0]


def _final_image(worker):
    """收尾成图（画布即自然朝向，无需任何翻转还原）。"""
    return worker._decode_stitched()


class TestScrollUpAfterScrollDown:
    """先下滑锁定 down，再往回滚——用户报告的场景。"""

    def test_rollback_keeps_canvas_then_prepends(self, qapp):
        worker = _new_worker()
        try:
            # 下滑 0,20,…,200 → 画布 [0, 300)
            for y0 in range(0, 201, STEP):
                payload = _submit(worker, y0)
                assert payload["ok"], f"下滑 y0={y0} 失败: {payload['error_detail']}"
            assert worker._locked == "down"
            assert worker._stitched_h == 300, worker._stitched_h
            down_h = worker._stitched_h

            # 回滚过已拼区域：画布必须原样，不能被裁短，也不该报错
            for y0 in range(180, -1, -STEP):
                payload = _submit(worker, y0)
                assert payload["ok"], f"回滚 y0={y0} 被判失败: {payload['error_detail']}"
                assert worker._stitched_h == down_h, (
                    f"y0={y0} 画布被裁短 {down_h} -> {worker._stitched_h}"
                )
            assert worker._locked == "down", "已拼区域回滚不该翻方向锁"
            assert worker._count == 11, "无新内容的帧不该计入总数"

            # 越过画布顶部：新内容要接到「上方」，方向锁随之翻转
            payload = _submit(worker, -20)
            assert payload["ok"], payload["error_detail"]
            assert worker._locked == "up", "未检测到滚动方向反转"
            assert worker._stitched_h == 320, worker._stitched_h

            payload = _submit(worker, -40)
            assert payload["ok"], payload["error_detail"]
            assert worker._locked == "up"
            assert worker._stitched_h == 340, worker._stitched_h
            assert worker._count == 13

            # 收尾成图逐行连续覆盖 [-40, 300)：一行都不能重复、不能错位
            result = _final_image(worker)
            assert result.size == (W, 340), result.size
            px = result.convert("RGB").load()
            for idx in range(340):
                expected_y = idx - 40
                assert px[5, idx] == row_rgb(expected_y), (
                    f"行 {idx}: 期望内容 {expected_y} {row_rgb(expected_y)} "
                    f"实际 {px[5, idx]}"
                )
        finally:
            worker.stop()


class TestScrollDownAfterScrollUp:
    """先上滑锁定 up，再往回滚到画布下方——对称方向的同一类问题。"""

    def test_rollback_keeps_canvas_then_appends(self, qapp):
        worker = _new_worker()
        try:
            # 上滑 200,180,…,0：第 2 帧由自动检测锁成 up → 画布 [0, 300)
            for y0 in range(200, -1, -STEP):
                payload = _submit(worker, y0)
                assert payload["ok"], f"上滑 y0={y0} 失败: {payload['error_detail']}"
            assert worker._locked == "up", worker._locked
            assert worker._stitched_h == 300, worker._stitched_h
            up_h = worker._stitched_h

            # 回滚过已拼区域
            for y0 in range(20, 201, STEP):
                payload = _submit(worker, y0)
                assert payload["ok"], f"回滚 y0={y0} 被判失败: {payload['error_detail']}"
                assert worker._stitched_h == up_h, (
                    f"y0={y0} 画布被裁短 {up_h} -> {worker._stitched_h}"
                )
            assert worker._locked == "up", "已拼区域回滚不该翻方向锁"
            assert worker._count == 11, worker._count

            # 越过画布底部：新内容接回「下方」，方向锁翻回 down
            payload = _submit(worker, 220)
            assert payload["ok"], payload["error_detail"]
            assert worker._locked == "down", "未检测到滚动方向反转"
            assert worker._stitched_h == 320, worker._stitched_h

            # 收尾成图逐行连续覆盖 [0, 320)：一行都不能重复、不能错位
            result = _final_image(worker)
            assert result.size == (W, 320), result.size
            px = result.convert("RGB").load()
            for idx in range(320):
                assert px[5, idx] == row_rgb(idx), (
                    f"行 {idx}: 期望内容 {idx} {row_rgb(idx)} "
                    f"实际 {px[5, idx]}"
                )
        finally:
            worker.stop()
