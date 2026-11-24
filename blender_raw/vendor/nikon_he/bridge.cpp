// SPDX-License-Identifier: LGPL-2.1-or-later
// Small, replaceable C ABI for LibRaw camera calibration and Nikon HE decoding.
#include "libraw/libraw.h"
#include <cstdint>
#include <cstring>
#include <new>

#ifdef _WIN32
#define BR_API extern "C" __declspec(dllexport)
using PathChar = wchar_t;
#else
#define BR_API extern "C" __attribute__((visibility("default")))
using PathChar = char;
#endif

struct CameraFrame {
    libraw_processed_image_t *owner;
    const uint16_t *pixels;
    uint32_t width, height;
    float xyz_to_camera[9], as_shot_multipliers[4];
};

// Identification reads the current LibRaw color data without unpacking sensor pixels.
BR_API int br_camera_matrix(const PathChar *path, float matrix[9]) noexcept {
    try {
        LibRaw raw;
        int err = raw.open_file(path);
        if (err) return err;
        if (raw.imgdata.idata.colors != 3 ||
            std::strncmp(raw.imgdata.idata.cdesc, "RGB", 3) != 0)
            return LIBRAW_FILE_UNSUPPORTED;
        std::memcpy(matrix, raw.imgdata.color.cam_xyz, 9 * sizeof(float));
        return LIBRAW_SUCCESS;
    } catch (const std::bad_alloc &) {
        return LIBRAW_UNSUFFICIENT_MEMORY;
    } catch (...) {
        return LIBRAW_UNSPECIFIED_ERROR;
    }
}

BR_API int br_he_read(const PathChar *path, int half_size, CameraFrame *out) noexcept {
    *out = {};
    try {
        LibRaw raw;
        int err = raw.open_file(path);
        if (err) return err;
        libraw_decoder_info_t decoder{};
        err = raw.get_decoder_info(&decoder);
        if (err) return err;
        if (!decoder.decoder_name || std::strcmp(decoder.decoder_name, "nikon_he_load_raw()") != 0)
            return LIBRAW_FILE_UNSUPPORTED;

        err = raw.unpack();
        if (err) return err;
        std::memcpy(out->xyz_to_camera, raw.imgdata.color.cam_xyz, sizeof(out->xyz_to_camera));
        std::memcpy(out->as_shot_multipliers, raw.imgdata.color.cam_mul, sizeof(out->as_shot_multipliers));
        auto &params = raw.imgdata.params;
        params.half_size = half_size != 0;
        params.output_bps = 16;
        params.output_color = 0;  // Native camera primaries; Python applies the DCP/WB.
        params.gamm[0] = params.gamm[1] = 1;
        for (float &value : params.user_mul) value = 1.0f;
        params.highlight = 1;
        params.no_auto_bright = 1;
        err = raw.dcraw_process();
        if (err) return err;
        libraw_processed_image_t *image = raw.dcraw_make_mem_image(&err);
        if (!image) return err ? err : LIBRAW_UNSUFFICIENT_MEMORY;
        if (image->type != LIBRAW_IMAGE_BITMAP || image->bits != 16 || image->colors != 3 ||
            uint64_t(image->width) * image->height * 6 != image->data_size) {
            LibRaw::dcraw_clear_mem(image);
            return LIBRAW_DATA_ERROR;
        }
        out->owner = image;
        out->pixels = reinterpret_cast<const uint16_t *>(image->data);
        out->width = image->width;
        out->height = image->height;
        return LIBRAW_SUCCESS;
    } catch (const std::bad_alloc &) {
        return LIBRAW_UNSUFFICIENT_MEMORY;
    } catch (...) {
        return LIBRAW_UNSPECIFIED_ERROR;
    }
}

BR_API void br_he_free(CameraFrame *frame) noexcept {
    if (frame->owner) LibRaw::dcraw_clear_mem(frame->owner);
    *frame = {};
}

BR_API const char *br_he_error(int code) noexcept {
    return LibRaw::strerror(code);
}
