# -*- coding: utf-8 -*-
"""对齐精度回归：分段均值说「像」不算数，重叠区的真实像素才算数。

历史缺陷：落位只看行签名（8 段分段均值）的逐行复核得分，而得分公式是
「匹配行 − 0.5×未匹配行」——**重叠越长分越高**。在重复版式（列表、卡片、
表格、等宽分栏）里，往回挪一个重复周期的错位偏移往往拥有更长的重叠，
于是拿到更高的签名分、被当成正确答案：画布长高了，却把已有内容重复/丢掉
一段。这正是用户报的「自动识别拼接错误」。

现在两段式：

* ``_align_candidates`` 只负责**召回**——量化键投票 + 抽样逐行复核，给出
  得分前 K 的候选；
* ``_choose_dy`` 负责**裁决**——拿重叠区真实像素的平均绝对差判真伪；分数
  打平时按滚动惯性取离「上一帧位置 + 上一步步长」最近的那个。

行数据说明：每行用 (R, G) 唯一编码内容坐标 y，保证任意两行都不相似；
页面后段刻意做成周期重复，用来制造「两个偏移的像素分一样好」的歧义。
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


W, FH = 120, 200
STEP = 60
# 内容从这一行起变成周期 P 的重复版式（之前是逐行唯一的散列色）
PERIOD_START = 500
PERIOD = 40


def row_rgb(y):
    h = (y * 2654435761) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822519) & 0xFFFFFFFF
    h ^= h >> 13
    return (h >> 16) & 0xFF, (h >> 8) & 0xFF, 128


def page_rgb(y):
    """页面第 y 行的颜色：前段唯一，后段周期重复。"""
    if y < PERIOD_START:
        return row_rgb(y)
    return row_rgb(PERIOD_START + (y - PERIOD_START) % PERIOD)


def make_qimage(y0, h=FH):
    """视口内容 [y0, y0+h)，BGRA32（worker 按 BGRA 解）。"""
    buf = bytearray()
    for i in range(h):
        r, g, b = page_rgb(y0 + i)
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


def _submit(worker, y0, h=FH):
    done = threading.Event()
    out = []
    worker._emit = lambda payload: (out.append(payload), done.set())
    worker.submit(make_qimage(y0, h), "vertical")
    assert done.wait(20.0), f"y0={y0} 未在超时内回包"
    return out[0]


def _final_image(worker):
    return worker._decode_stitched()


class TestRepeatedBlockPlacement:
    """重复版式里的错位候选：签名分更高，但真实像素一眼就看得出不对。"""

    def test_pixel_stage_rejects_the_longer_overlap_lie(self, qapp):
        worker = _new_worker()
        try:
            # 逐行唯一的区域里连滚 5 帧：偏移无歧义，惯性被建立成 +60
            for y0 in range(0, 481, STEP):
                payload = _submit(worker, y0)
                assert payload["ok"], f"y0={y0} 失败: {payload['error_detail']}"
            assert worker._stitched_h == 480 + FH, worker._stitched_h
            assert worker._prev_delta == STEP

            # 进入重复区：真实 dy=540（重叠 140 行），而 dy=500 往回挪一个
            # 周期，重叠 180 行且**整段全中**——签名分 180 > 140，老算法会
            # 选它，画布只长到 700，正好丢掉一段内容
            payload = _submit(worker, 540)
            assert payload["ok"], payload["error_detail"]
            assert worker._stitched_h == 540 + FH, (
                f"选了重叠更长的错位偏移，画布 {worker._stitched_h} "
                f"应当是 {540 + FH}"
            )
            assert worker._locked == "down"

            # 逐行核对：一行都不能重复、不能错位
            result = _final_image(worker)
            assert result.size == (W, 540 + FH), result.size
            px = result.convert("RGB").load()
            for idx in range(540 + FH):
                assert px[5, idx] == page_rgb(idx), (
                    f"行 {idx}: 期望 {page_rgb(idx)} 实际 {px[5, idx]}"
                )
        finally:
            worker.stop()

    def test_alignment_stays_pixel_exact_after_many_repeated_frames(self, qapp):
        """连着滚进重复区也不该越拼越偏。"""
        worker = _new_worker()
        try:
            for y0 in range(0, 601, STEP):
                payload = _submit(worker, y0)
                assert payload["ok"], f"y0={y0}: {payload['error_detail']}"
            assert worker._stitched_h == 600 + FH, worker._stitched_h
            result = _final_image(worker)
            px = result.convert("RGB").load()
            for idx in range(600 + FH):
                assert px[5, idx] == page_rgb(idx), f"行 {idx} 内容错位"
        finally:
            worker.stop()


class TestInertiaTieBreak:
    """两个偏移的像素分一样好时，按滚动惯性裁决。"""

    def test_equal_scores_pick_where_the_scroll_was_heading_for(self):
        worker = _new_worker()
        try:
            worker._prev_row = 480
            worker._prev_delta = 60
            worker._pixel_score = lambda frame, dy, skip_top, final: 0.0
            chosen = worker._choose_dy(None, [(540, 100.0), (500, 100.0)], 0)
            assert chosen == 540, "惯性已经建立，同分时该取 480+60"
        finally:
            worker.stop()

    def test_equal_scores_without_inertia_keep_the_signature_winner(self):
        """惯性还没建立（第二帧）：同分时信签名给的原始候选，不要乱挪。"""
        worker = _new_worker()
        try:
            worker._prev_row = 0
            worker._prev_delta = 0
            worker._pixel_score = lambda frame, dy, skip_top, final: 0.0
            chosen = worker._choose_dy(None, [(120, 90.0), (60, 80.0)], 0)
            assert chosen == 120, "签名得分更高的原始候选应当保持不变"
        finally:
            worker.stop()


class TestPixelRejection:
    """真实像素全盘否定时，宁可交给 Rust 兜底，也不把错位写进画布。"""

    def test_unmatchable_overlap_goes_to_the_rust_fallback(self, qapp, monkeypatch):
        worker = _new_worker()
        try:
            payload = _submit(worker, 0)
            assert payload["ok"], payload["error_detail"]

            calls = []
            monkeypatch.setattr(
                type(worker),
                "_rust_fallback",
                lambda self, im: (calls.append(im.size), "fail")[1],
            )
            monkeypatch.setattr(
                type(worker),
                "_pixel_score",
                lambda self, frame, dy, skip_top, final: 999.0,
            )

            payload = _submit(worker, STEP)
            assert calls, "像素裁决全盘否定时必须交回 Rust 兜底"
            assert payload["ok"] is False
            assert worker._stitched_h == FH, "失败帧不该改动画布"
        finally:
            worker.stop()


class TestSampledVerification:
    """长重叠走抽样复核：开销与画布长度脱钩，但正确偏移不能漏。"""

    def test_long_overlap_is_still_aligned_correctly(self, qapp):
        from PIL import Image

        worker = _new_worker()
        try:
            def pil(y0, h=600):
                buf = bytearray()
                for i in range(h):
                    r, g, b = page_rgb(y0 + i)
                    buf += bytes((r, g, b)) * W
                return Image.frombytes("RGB", (W, h), bytes(buf))

            worker._set_canvas(pil(0))
            frame = pil(100)
            sig = worker._row_signatures(frame)
            # 重叠 500 行 > _SIG_VERIFY_ROWS，触发抽样（step ≥ 2）
            candidates = worker._align_candidates(sig, 0)
            assert candidates, "抽样复核不该把正确偏移漏掉"
            assert candidates[0][0] == 100, candidates[:3]
        finally:
            worker.stop()


class TestRustAlignerParity:
    """j-stitch Aligner（原生投票+校验）与纯 Python 分支的语义对照。

    _align_candidates 有两条实现：j-stitch ≥ Aligner 时走原生，旧版回落
    纯 Python。两条路径必须逐候选一致（含平票的首见序），否则重复版式
    里的惯性裁决会随打包环境漂移。
    """

    @pytest.fixture
    def worker(self):
        from stitch.scroll_window import _StitchWorker

        w = _StitchWorker(
            scroll_direction="vertical",
            locked_direction=None,
            duplicate_threshold=0.95,
            emit_fn=lambda payload: None,
        )
        yield w
        w.stop()

    @staticmethod
    def _cases():
        import random

        rng = random.Random(42)

        def rand_sig(rows):
            return bytes(rng.randrange(256) for _ in range(rows * 24))

        base = rand_sig(150)
        periodic = rand_sig(20) * 8
        shifted = bytes(10 * 24) + base[60 * 24:]
        return [
            ("随机噪声-惯性兜底", rand_sig(120), rand_sig(30), 0, 90),
            ("真实移位重叠", base, shifted, 0, 50),
            ("重复版式平票", periodic, periodic[40 * 24:] + periodic[:40 * 24], 0, 90),
            ("skip_top 生效", base, shifted, 20, 50),
            ("画布比帧短", base[:40 * 24], base[20 * 24:], 0, 10),
            ("expected 越界", base, shifted, 0, 500),
            ("负 expected", base, shifted, 0, -30),
        ]

    def test_rust_matches_python_branch(self, worker):

        assert worker._aligner is not None, "测试环境应装配原生 Aligner"
        for name, canvas, frame, skip, expected in self._cases():
            worker._canvas_sig = canvas
            worker._prev_row, worker._prev_delta = 0, expected

            rust = worker._aligner.find_candidates(canvas, frame, skip, expected)
            saved = worker._aligner
            worker._aligner = None
            py = worker._align_candidates(frame, skip)
            worker._aligner = saved

            assert rust == py, f"{name}: 原生与 Python 分支不一致\n  原生={rust}\n  Py={py}"

    def test_rust_path_is_actually_used(self, worker):
        """装配了 Aligner 时 _align_candidates 必须走原生路径（而非静默回落）。"""
        assert worker._aligner is not None

        class _SpyAligner:
            """PyO3 frozen 类的方法只读，包一层记调用的替身。"""

            def __init__(self, real):
                self._real = real
                self.calls = []

            def find_candidates(self, *args, **kwargs):
                self.calls.append(args)
                return self._real.find_candidates(*args, **kwargs)

        spy = _SpyAligner(worker._aligner)
        worker._aligner = spy
        try:
            worker._canvas_sig = self._cases()[1][1]
            worker._align_candidates(self._cases()[1][2], 0)
            assert spy.calls, "候选召回应经由 j-stitch Aligner"
        finally:
            worker._aligner = spy._real
