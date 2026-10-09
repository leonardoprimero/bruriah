//! Helpers for loading [`egui::IconData`].

use egui::IconData;

/// Helpers for working with [`IconData`].
pub trait IconDataExt {
    /// Convert into [`image::RgbaImage`]
    ///
    /// # Errors
    /// If `width*height != 4 * rgba.len()`, or if the image is too big.
    fn to_image(&self) -> Result<image::RgbaImage, String>;

    /// Encode as PNG.
    ///
    /// # Errors
    /// The image is invalid, or the PNG encoder failed.
    fn to_png_bytes(&self) -> Result<Vec<u8>, String>;
}

/// Load the contents of .png file.
///
/// Only PNG is decoded here. For any other format, decode the icon in your application
/// and pass the pixels to [`from_rgba`].
///
/// # Errors
/// If this is not a valid png.
pub fn from_png_bytes(png_bytes: &[u8]) -> Result<IconData, image::ImageError> {
    profiling::function_scope!();
    let image = image::load_from_memory(png_bytes)?;
    Ok(from_image(image))
}

/// Create an icon from decoded, unmultiplied RGBA pixels, e.g. from a JPEG or ICO file your
/// application decoded itself.
///
/// # Errors
/// If `rgba.len()` is not `4 * width * height`.
pub fn from_rgba(rgba: Vec<u8>, width: u32, height: u32) -> Result<IconData, String> {
    let expected = 4 * width as usize * height as usize;
    if rgba.len() != expected {
        return Err(format!(
            "Expected {expected} bytes of RGBA for a {width}x{height} icon, got {}",
            rgba.len()
        ));
    }
    Ok(IconData {
        width,
        height,
        rgba,
    })
}

fn from_image(image: image::DynamicImage) -> IconData {
    let image = image.into_rgba8();
    IconData {
        width: image.width(),
        height: image.height(),
        rgba: image.into_raw(),
    }
}

impl IconDataExt for IconData {
    fn to_image(&self) -> Result<image::RgbaImage, String> {
        profiling::function_scope!();
        let Self {
            rgba,
            width,
            height,
        } = self.clone();
        image::RgbaImage::from_raw(width, height, rgba).ok_or_else(|| "Invalid IconData".to_owned())
    }

    fn to_png_bytes(&self) -> Result<Vec<u8>, String> {
        profiling::function_scope!();
        let image = self.to_image()?;
        let mut png_bytes: Vec<u8> = Vec::new();
        image
            .write_to(
                &mut std::io::Cursor::new(&mut png_bytes),
                image::ImageFormat::Png,
            )
            .map_err(|err| err.to_string())?;
        Ok(png_bytes)
    }
}
