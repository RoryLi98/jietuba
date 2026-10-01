//! 剪贴板图片格式准备：DIBV5（BITMAPV5HEADER + bottom-up 像素）与 PNG。
//!
//! Python 旧实现（core/clipboard_utils._build_dibv5/_build_png）走 Qt mirrored
//! 翻转 + bytes() 取像素（两次全图拷贝）+ Qt libpng 编码。这里从同一份
//! BGRA 裸像素单趟完成翻转与打包，PNG 用 png crate 的快速压缩，4K 截图
//! 整体耗时约为 Qt 路径的三分之一。

use std::io::BufWriter;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const BITMAPV5HEADER_SIZE: u32 = 124;

/// 从 ARGB32（QImage 内存布局 BGRA）裸像素构造 (DIBV5, PNG)。
///
/// 薄包装见 lib.rs 的 `prepare_image_formats`（PyBuffer 解引用 + PyBytes 封装）。
pub fn prepare_from_slice(pixels: &[u8], width: usize, height: usize) -> PyResult<(Vec<u8>, Vec<u8>)> {
    let expected = width
        .checked_mul(4)
        .and_then(|stride| stride.checked_mul(height))
        .ok_or_else(|| PyValueError::new_err("图片尺寸溢出"))?;
    if pixels.len() != expected {
        return Err(PyValueError::new_err(format!(
            "像素数据长度 {} 与 {}x{}x4 不符",
            pixels.len(),
            width,
            height
        )));
    }
    let dibv5 = build_dibv5(pixels, width, height);
    let png = encode_png(pixels, width, height)?;
    Ok((dibv5, png))
}

/// BITMAPV5HEADER + bottom-up 像素，单趟完成（行序倒转即翻转）。
fn build_dibv5(bgra: &[u8], width: usize, height: usize) -> Vec<u8> {
    let pixel_size = width * height * 4;
    let mut out = Vec::with_capacity(BITMAPV5HEADER_SIZE as usize + pixel_size);
    // 字段与 clipboard_utils._build_dibv5 的 struct.pack 序列一致
    out.extend_from_slice(&BITMAPV5HEADER_SIZE.to_le_bytes()); // bV5Size
    out.extend_from_slice(&(width as i32).to_le_bytes()); // bV5Width
    out.extend_from_slice(&(height as i32).to_le_bytes()); // bV5Height（正 = bottom-up）
    out.extend_from_slice(&1u16.to_le_bytes()); // bV5Planes
    out.extend_from_slice(&32u16.to_le_bytes()); // bV5BitCount
    out.extend_from_slice(&3u32.to_le_bytes()); // bV5Compression = BI_BITFIELDS
    out.extend_from_slice(&(pixel_size as u32).to_le_bytes()); // bV5SizeImage
    out.extend_from_slice(&0i32.to_le_bytes()); // bV5XPelsPerMeter
    out.extend_from_slice(&0i32.to_le_bytes()); // bV5YPelsPerMeter
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5ClrUsed
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5ClrImportant
    out.extend_from_slice(&0x00FF0000u32.to_le_bytes()); // bV5RedMask
    out.extend_from_slice(&0x0000FF00u32.to_le_bytes()); // bV5GreenMask
    out.extend_from_slice(&0x000000FFu32.to_le_bytes()); // bV5BlueMask
    out.extend_from_slice(&0xFF000000u32.to_le_bytes()); // bV5AlphaMask
    out.extend_from_slice(&0x73524742u32.to_le_bytes()); // bV5CSType = LCS_sRGB
    out.extend_from_slice(&[0u8; 36]); // bV5Endpoints (CIEXYZTRIPLE)
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5GammaRed
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5GammaGreen
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5GammaBlue
    out.extend_from_slice(&4u32.to_le_bytes()); // bV5Intent = LCS_GM_IMAGES
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5ProfileData
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5ProfileSize
    out.extend_from_slice(&0u32.to_le_bytes()); // bV5Reserved
    debug_assert_eq!(out.len(), 124);

    // 像素：bottom-up（倒着拷行即完成翻转，无中间缓冲）
    let stride = width * 4;
    for row in (0..height).rev() {
        let start = row * stride;
        out.extend_from_slice(&bgra[start..start + stride]);
    }
    out
}

/// BGRA → RGBA → PNG（png crate 快速压缩，IEND 随 Writer 收尾写入）。
fn encode_png(bgra: &[u8], width: usize, height: usize) -> PyResult<Vec<u8>> {
    let stride = width * 4;
    let mut rgba = Vec::with_capacity(bgra.len());
    for row in 0..height {
        for px in bgra[row * stride..(row + 1) * stride].chunks_exact(4) {
            rgba.push(px[2]);
            rgba.push(px[1]);
            rgba.push(px[0]);
            rgba.push(px[3]);
        }
    }

    let mut out = Vec::new();
    {
        let mut encoder =
            png::Encoder::new(BufWriter::new(&mut out), width as u32, height as u32);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_compression(png::Compression::Fast);
        let mut writer = encoder
            .write_header()
            .map_err(|e| PyValueError::new_err(format!("PNG 头写入失败: {e}")))?;
        writer
            .write_image_data(&rgba)
            .map_err(|e| PyValueError::new_err(format!("PNG 编码失败: {e}")))?;
        writer
            .finish()
            .map_err(|e| PyValueError::new_err(format!("PNG 收尾失败: {e}")))?;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 2×2 测试图（BGRA）：上行 ABGR 值 1/2，下行 3/4。
    fn sample() -> Vec<u8> {
        let mut v = Vec::new();
        for (r, g, b, a) in [(1, 10, 100, 255), (2, 20, 200, 255), (3, 30, 3, 255), (4, 40, 4, 255)] {
            v.extend_from_slice(&[b, g, r, a]);
        }
        v
    }

    #[test]
    fn dibv5_header_and_row_order() {
        let out = build_dibv5(&sample(), 2, 2);
        assert_eq!(out.len(), 124 + 16);
        assert_eq!(u32::from_le_bytes(out[0..4].try_into().unwrap()), 124);
        assert_eq!(i32::from_le_bytes(out[4..8].try_into().unwrap()), 2);
        assert_eq!(i32::from_le_bytes(out[8..12].try_into().unwrap()), 2); // 正高 = bottom-up
        // bottom-up：首个像素行应是「最后一行」（行 1 = 像素 2,3）
        assert_eq!(&out[124..132], &[3, 30, 3, 255, 4, 40, 4, 255]);
        // 末个像素行应是「第一行」（像素 0,1）
        assert_eq!(&out[132..140], &[100, 10, 1, 255, 200, 20, 2, 255]);
    }

    #[test]
    fn png_is_valid_and_preserves_dimensions() {
        let out = encode_png(&sample(), 2, 2).unwrap();
        assert_eq!(&out[..8], b"\x89PNG\r\n\x1a\n");
        let decoded = image::load_from_memory(&out).unwrap();
        assert_eq!((decoded.width(), decoded.height()), (2, 2));
        // BGRA→RGBA 通道换位正确：左上像素 R=1, G=10, B=100
        let rgba = decoded.to_rgba8();
        let px = rgba.get_pixel(0, 0);
        assert_eq!((px[0], px[1], px[2], px[3]), (1, 10, 100, 255));
    }

    #[test]
    fn length_mismatch_is_rejected() {
        let result = prepare_from_slice(&[0u8; 12], 2, 2);
        // 长度 12 ≠ 2*2*4：应报错
        assert!(result.is_err());
    }
}
