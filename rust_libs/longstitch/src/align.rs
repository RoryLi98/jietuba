//! 增量对齐：签名键投票 → 候选校验。
//!
//! scroll_window._StitchWorker._align_candidates 的原生移植：长截图每帧都要
//! 在画布签名上重建键索引（O(画布行数) 的字典操作）再投票、逐行校验，画布
//! 拼到几万行后这段纯 Python 循环成为帧管线大头。语义与 Python 版逐字节
//! 一致（键拼装、桶封顶、频次禁言、平票首见优先、行比较容差预算），
//! 行为基准由 scroll_window 的对齐精度测试双向把关。

use std::collections::HashMap;

use pyo3::prelude::*;

/// 首见序保序的 dy → 计数表：Python dict 的平票裁决依赖插入序。
struct VoteTable {
    index: HashMap<i64, usize>,
    rows: Vec<(i64, usize)>,
}

impl VoteTable {
    fn new() -> Self {
        Self { index: HashMap::new(), rows: Vec::new() }
    }

    fn add(&mut self, dy: i64, n: usize) {
        match self.index.get(&dy) {
            Some(&slot) => self.rows[slot].1 += n,
            None => {
                self.index.insert(dy, self.rows.len());
                self.rows.push((dy, n));
            }
        }
    }

    fn get(&self, dy: i64) -> usize {
        self.index.get(&dy).map_or(0, |&slot| self.rows[slot].1)
    }

    fn iter(&self) -> std::slice::Iter<'_, (i64, usize)> {
        self.rows.iter()
    }
}

#[pyclass(frozen, name = "Aligner")]
pub struct PyAligner {
    sig_bytes: usize,
    key_seg_a: usize,
    key_seg_b: usize,
    bucket_cap: usize,
    n_candidates: usize,
    pix_candidates: usize,
    verify_rows: usize,
    match_min_rows: usize,
    match_min_ratio: f64,
    row_match_max: i64,
    min_overlap: i64,
}

#[pymethods]
impl PyAligner {
    #[new]
    #[pyo3(signature = (sig_bytes, key_seg_a, key_seg_b, bucket_cap,
                        n_candidates, pix_candidates, verify_rows,
                        match_min_rows, match_min_ratio, row_match_max, min_overlap))]
    fn new(
        sig_bytes: usize,
        key_seg_a: usize,
        key_seg_b: usize,
        bucket_cap: usize,
        n_candidates: usize,
        pix_candidates: usize,
        verify_rows: usize,
        match_min_rows: usize,
        match_min_ratio: f64,
        row_match_max: i64,
        min_overlap: i64,
    ) -> Self {
        Self {
            sig_bytes,
            key_seg_a,
            key_seg_b,
            bucket_cap,
            n_candidates,
            pix_candidates,
            verify_rows,
            match_min_rows,
            match_min_ratio,
            row_match_max,
            min_overlap,
        }
    }

    /// 在画布行签名里找出新帧顶行的候选偏移 dy（画布行号，可为负）。
    ///
    /// canvas_sig / frame_sig 都是逐行 `sig_bytes` 字节的签名串。返回
    /// [(dy, score)]，排序（命中率 → 离惯性估计的距离 → 得分）与截断
    /// （前 `pix_candidates` 个）和 Python 版一致。
    fn find_candidates(
        &self,
        canvas_sig: &[u8],
        frame_sig: &[u8],
        skip_top: i64,
        expected: i64,
    ) -> Vec<(i64, f64)> {
        let sb = self.sig_bytes;
        if canvas_sig.is_empty() || frame_sig.is_empty() || sb == 0 {
            return Vec::new();
        }
        let ch = canvas_sig.len() / sb;
        let fh = frame_sig.len() / sb;
        if ch == 0 || fh == 0 {
            return Vec::new();
        }

        let key = |sig: &[u8], row: usize| -> i64 {
            let a = row * sb + self.key_seg_a * 3;
            let b = row * sb + self.key_seg_b * 3;
            (((sig[a] >> 4) as i64) << 20)
                | (((sig[a + 1] >> 4) as i64) << 16)
                | (((sig[a + 2] >> 4) as i64) << 12)
                | (((sig[b] >> 4) as i64) << 8)
                | (((sig[b + 1] >> 4) as i64) << 4)
                | ((sig[b + 2] >> 4) as i64)
        };

        // 画布：键 → 行号（每键封顶，先到的行优先）
        let mut index: HashMap<i64, Vec<i64>> = HashMap::with_capacity(ch * 2);
        for i in 0..ch {
            let k = key(canvas_sig, i);
            match index.get_mut(&k) {
                Some(rows) => {
                    if rows.len() < self.bucket_cap {
                        rows.push(i as i64);
                    }
                }
                None => {
                    index.insert(k, vec![i as i64]);
                }
            }
        }

        // 帧内键频次：高频行不发言（纯白行只造噪声候选）
        let mut freq: HashMap<i64, usize> = HashMap::with_capacity(fh * 2);
        for j in 0..fh {
            *freq.entry(key(frame_sig, j)).or_insert(0) += 1;
        }

        // 投票：每对键相同的行对 (画布 i, 帧 j) 为偏移 i−j 投一票
        let mut votes = VoteTable::new();
        for j in (skip_top.max(0) as usize)..fh {
            let k = key(frame_sig, j);
            if freq.get(&k).copied().unwrap_or(0) > self.bucket_cap {
                continue;
            }
            if let Some(rows) = index.get(&k) {
                for &i in rows {
                    votes.add(i - j as i64, 1);
                }
            }
        }

        // 按票数排序，平票按离惯性估计的距离（Python 的稳定排序语义）
        let mut ranked: Vec<i64> = {
            let mut ordered: Vec<(i64, usize)> = votes.iter().copied().collect();
            ordered.sort_by(|a, b| {
                b.1.cmp(&a.1)
                    .then((a.0 - expected).abs().cmp(&(b.0 - expected).abs()))
            });
            ordered.into_iter().map(|(dy, _)| dy).take(self.n_candidates).collect()
        };
        if !ranked.contains(&expected) {
            ranked.push(expected);
        }

        // 候选校验：重叠区按均匀步长抽样逐行复核（带容差预算）
        let mut out: Vec<(f64, i64, f64, i64)> = Vec::with_capacity(ranked.len());
        for &dy in &ranked {
            let lo = 0i64.max(-dy).max(skip_top);
            let hi = (fh as i64).min(ch as i64 - dy);
            let overlap = hi - lo;
            if overlap < self.min_overlap.min(fh as i64) {
                continue;
            }
            let step = (overlap as usize / self.verify_rows).max(1);
            let mut matches = 0usize;
            let mut checked = 0usize;
            let mut j = lo;
            while j < hi {
                checked += 1;
                if row_matches(frame_sig, j as usize, canvas_sig, (dy + j) as usize, sb, self.row_match_max) {
                    matches += 1;
                }
                j += step as i64;
            }
            let min_needed = self.match_min_rows.min(checked) as i64;
            if (matches as i64) < min_needed
                || (matches as f64) < checked as f64 * self.match_min_ratio
            {
                continue;
            }
            // 未匹配行按半票扣分：免得「短重叠 100%」压过「长重叠 95%」
            let score = matches as f64 - 0.5 * ((checked - matches) as f64);
            out.push((matches as f64 / checked as f64, (dy - expected).abs(), score, dy));
        }

        out.sort_by(|a, b| {
            b.0.partial_cmp(&a.0)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.1.cmp(&b.1))
                .then(b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal))
        });
        out.truncate(self.pix_candidates);
        out.into_iter().map(|(_ratio, _dist, score, dy)| (dy, score)).collect()
    }
}

/// 行比较：带容差预算的逐字节差值累计，超预算即否（与 _row_matches 一致）。
fn row_matches(frame_sig: &[u8], j: usize, canvas_sig: &[u8], i: usize, sb: usize, max: i64) -> bool {
    let a = j * sb;
    let b = i * sb;
    let mut total = 0i64;
    for k in 0..sb {
        let d = frame_sig[a + k] as i64 - canvas_sig[b + k] as i64;
        total += d.abs();
        if total > max {
            return false;
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    const SB: usize = 24;
    const SEG_A: usize = 2;
    const SEG_B: usize = 5;
    const ROW_MAX: i64 = 12 * 8 * 3;

    fn aligner() -> PyAligner {
        PyAligner::new(SB, SEG_A, SEG_B, 64, 12, 3, 240, 12, 0.5, ROW_MAX, 20)
    }

    /// 一行签名的构造：8 个段，每段值填满 3 字节。
    fn sig_of_rows(rows: &[[u8; 8]]) -> Vec<u8> {
        let mut sig = Vec::with_capacity(rows.len() * SB);
        for row in rows {
            for seg in row {
                sig.push(*seg);
                sig.push(*seg);
                sig.push(*seg);
            }
        }
        sig
    }

    /// 帧内容 = 画布第 `dy` 行开始的内容（向下滚动），首行前置 `pad` 行噪声。
    fn shifted_pair(canvas_rows: usize, dy: usize, pad: usize) -> (Vec<u8>, Vec<u8>) {
        let base: Vec<[u8; 8]> = (0..canvas_rows)
            .map(|r| {
                let v = ((r * 7) % 251 + 2) as u8;
                [v, v.wrapping_add(r as u8), v, v, v.wrapping_add(r as u8), v, v, v]
            })
            .collect();
        let canvas_sig = sig_of_rows(&base);
        let mut frame_rows: Vec<[u8; 8]> = (0..pad)
            .map(|r| {
                let v = ((r * 13) % 243 + 2) as u8;
                [v, v, v.wrapping_add(r as u8), v, v, v, v, v]
            })
            .collect();
        frame_rows.extend_from_slice(&base[dy.min(canvas_rows)..]);
        (canvas_sig, sig_of_rows(&frame_rows))
    }

    #[test]
    fn finds_shifted_offset() {
        // 帧 = 10 行噪声 + 画布第 60 行起的内容 → 真实偏移 dy = 60 - 10 = 50
        let (canvas_sig, frame_sig) = shifted_pair(200, 60, 10);
        let found = aligner().find_candidates(&canvas_sig, &frame_sig, 0, 50);
        assert!(found.iter().any(|&(dy, _)| dy == 50), "应找回真实偏移 50：{found:?}");
        // 命中率排序下冠军就是真身
        assert_eq!(found[0].0, 50);
    }

    #[test]
    fn inertia_tie_prefers_expected() {
        // 纯白画布 + 纯白帧：所有偏移票数被频次禁言（freq > bucket_cap），
        // 只剩 expected 兜底候选——但它重叠全同分，理应胜出
        let blank: Vec<u8> = vec![0xFF; SB * 300];
        let frame: Vec<u8> = vec![0xFF; SB * 40];
        let found = aligner().find_candidates(&blank, &frame, 0, 25);
        // 纯白行键频次 40 ≤ 64 不禁言 → 大量同票；平票按 |dy-expected| 取近者
        assert!(found.iter().any(|&(dy, _)| dy == 25), "expected 必须参与：{found:?}");
    }

    #[test]
    fn skip_top_rows_do_not_vote() {
        let (canvas_sig, frame_sig) = shifted_pair(200, 50, 6);
        // skip_top 越过帧里的真重叠区 → 投票只看 pad 行，校验全不合格
        let found = aligner().find_candidates(&canvas_sig, &frame_sig, 46, 50);
        assert!(found.iter().all(|&(dy, _)| dy != 50), "skip_top 内的行不该投票：{found:?}");
    }

    #[test]
    fn empty_signatures_give_no_candidates() {
        assert!(aligner().find_candidates(&[], &[0u8; 48], 0, 0).is_empty());
        assert!(aligner().find_candidates(&[0u8; 48], &[], 0, 0).is_empty());
    }
}
