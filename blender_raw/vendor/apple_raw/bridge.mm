// SPDX-License-Identifier: GPL-3.0-or-later
#import <CoreImage/CoreImage.h>
#import <Foundation/Foundation.h>
#import <ImageIO/CGImageProperties.h>

#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <memory>

// The caller owns the top-down, straight RGBA float buffer on success.
extern "C" int br_apple_raw_read_v3(const char *path, int wb, float temperature, float tint,
                                    int lensCorrection, float sampleX, float sampleY, int maxSize, int zeroShadowBias,
                                    float **pixels, int *width, int *height, float *appliedWB,
                                    char *error, size_t capacity) {
    @autoreleasepool {
        @try {
            auto fail = [&](const char *message) {
                std::snprintf(error, capacity, "%s", message);
                return 0;
            };
            NSString *name = [[NSFileManager defaultManager]
                stringWithFileSystemRepresentation:path length:std::strlen(path)];
            CIRAWFilter *raw = [CIRAWFilter filterWithImageURL:[NSURL fileURLWithPath:name]];
            if (!raw.decoderVersion || [raw.decoderVersion isEqualToString:CIRAWDecoderVersionNone] ||
                raw.nativeSize.width <= 0 || raw.nativeSize.height <= 0) {
                fail("This camera or RAW format is unsupported by Apple Core Image");
                return -1;
            }

            int orientation = [raw.properties[(__bridge NSString *)kCGImagePropertyOrientation] intValue];
            raw.orientation = (CGImagePropertyOrientation)(orientation >= 1 && orientation <= 8 ? orientation : 1);
            raw.boostAmount = 0;
            raw.boostShadowAmount = 1;
            // Core Image's Sigma FP2 profile subtracts shadows even with its tone curve off.
            if (zeroShadowBias) raw.shadowBias = 0;
            raw.exposure = 0;
            raw.gamutMappingEnabled = NO;
            raw.extendedDynamicRangeAmount = 1;
            if (raw.localToneMapSupported) raw.localToneMapAmount = 0;
            if (raw.luminanceNoiseReductionSupported) raw.luminanceNoiseReductionAmount = 0;
            if (raw.colorNoiseReductionSupported) raw.colorNoiseReductionAmount = 0;
            if (raw.sharpnessSupported) raw.sharpnessAmount = 0;
            if (raw.lensCorrectionSupported) raw.lensCorrectionEnabled = lensCorrection != 0;
            if (wb == 3) {
                CGRect extent = raw.outputImage.extent;
                // The Blender picker returns normalized top-down image coordinates.
                // Core Image expects pixel coordinates in its upright, bottom-up extent.
                raw.neutralLocation = CGPointMake(extent.origin.x + sampleX * (extent.size.width - 1),
                                                  extent.origin.y + (1 - sampleY) * (extent.size.height - 1));
            } else if (wb != 0) {
                raw.neutralTemperature = wb == 1 ? 6504 : temperature;
                raw.neutralTint = wb == 1 ? 0 : tint;
            }
            if (maxSize > 0) {
                CGFloat longest = std::fmax(raw.nativeSize.width, raw.nativeSize.height);
                if (longest > maxSize) raw.scaleFactor = maxSize / longest;
            }

            CIImage *image = raw.outputImage;
            CGRect extent = image.extent;
            if (!image || CGRectIsEmpty(extent) || !std::isfinite(extent.size.width) ||
                !std::isfinite(extent.size.height) ||
                extent.size.width > INT_MAX || extent.size.height > INT_MAX) {
                fail("Core Image could not develop this RAW image");
                return -1;
            }
            int w = (int)extent.size.width, h = (int)extent.size.height;
            if (size_t(w) * h > std::numeric_limits<size_t>::max() / (4 * sizeof(float))) {
                return fail("RAW image dimensions exceed the addressable image size");
            }
            size_t rowBytes = size_t(w) * 4 * sizeof(float);
            std::unique_ptr<float, decltype(&free)> buffer((float *)calloc(h, rowBytes), &free);
            if (!buffer) return fail("Insufficient memory for the decoded RAW image");

            id space = CFBridgingRelease(CGColorSpaceCreateWithName(kCGColorSpaceExtendedLinearITUR_2020));
            CIContext *context = [CIContext contextWithOptions:@{
                kCIContextWorkingColorSpace: space, kCIContextWorkingFormat: @(kCIFormatRGBAf)}];
            [context render:image toBitmap:buffer.get() rowBytes:rowBytes bounds:extent
                      format:kCIFormatRGBAf colorSpace:(__bridge CGColorSpaceRef)space];
            // A failed Core Image render can silently leave the entire bitmap untouched.
            if (!(buffer.get()[((size_t(h) / 2) * w + w / 2) * 4 + 3] > 0)) {
                return fail("Core Image returned no RAW pixels; check macOS camera and GPU support");
            }
            *pixels = buffer.release();
            *width = w;
            *height = h;
            appliedWB[0] = raw.neutralTemperature;
            appliedWB[1] = raw.neutralTint;
            return 1;
        } @catch (NSException *exception) {
            std::snprintf(error, capacity, "%s", exception.reason.UTF8String);
            return 0;
        }
    }
}

extern "C" void br_apple_raw_free(float *pixels) {
    free(pixels);
}
