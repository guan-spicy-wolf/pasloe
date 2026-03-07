use image::{RgbaImage, imageops};

pub fn dHash(image: &RgbaImage, _resolution: u32) -> u64 {
    //TODO: really use resolution
    let resolution = 8;
    let resized = imageops::resize(
        image,
        resolution + 1,
        resolution,
        imageops::FilterType::Lanczos3,
    );
    let gray = imageops::grayscale(&resized);

    let mut hash = 0u64;
    for y in 0..resolution {
        for x in 0..resolution {
            let left = gray.get_pixel(x, y)[0];
            let right = gray.get_pixel(x + 1, y)[0];
            if left < right {
                hash |= 1 << (y * resolution + x);
            }
        }
    }
    hash
}

pub fn ssim(img1: &RgbaImage, img2: &RgbaImage) -> f64 {
    let gray1 = imageops::grayscale(img1);
    let gray2 = imageops::grayscale(img2);

    let (w, h) = (
        gray1.width().min(gray2.width()),
        gray1.height().min(gray2.height()),
    );

    let (mut sum1, mut sum2, mut sum_sq1, mut sum_sq2, mut sum_12) = (0.0, 0.0, 0.0, 0.0, 0.0);
    let n = (w * h) as f64;

    for y in 0..h {
        for x in 0..w {
            let p1 = gray1.get_pixel(x, y)[0] as f64;
            let p2 = gray2.get_pixel(x, y)[0] as f64;
            sum1 += p1;
            sum2 += p2;
            sum_sq1 += p1 * p1;
            sum_sq2 += p2 * p2;
            sum_12 += p1 * p2;
        }
    }

    let mean1 = sum1 / n;
    let mean2 = sum2 / n;
    let var1 = sum_sq1 / n - mean1 * mean1;
    let var2 = sum_sq2 / n - mean2 * mean2;
    let covar = sum_12 / n - mean1 * mean2;

    let c1 = 6.5025;
    let c2 = 58.5225;

    ((2.0 * mean1 * mean2 + c1) * (2.0 * covar + c2))
        / ((mean1 * mean1 + mean2 * mean2 + c1) * (var1 + var2 + c2))
}

pub fn hamming_distance(hash1: u64, hash2: u64) -> u32 {
    (hash1 ^ hash2).count_ones()
}
