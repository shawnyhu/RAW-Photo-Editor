// SPDX-License-Identifier: GPL-3.0-or-later
// Keep the upstream, revision-specific render parameter layout inside C++.
#include "SpektraAppBridge.h"
#include "SpektraProfileCurves.h"
#include <algorithm>
#include <cstdio>
#include <exception>
#include <memory>
#ifdef _WIN32
#include <string>
namespace spektrafilm {
bool probeVulkanRenderer(std::string &message);
}

extern "C" int photo_film_probe(char *message, size_t size) {
  try {
    std::string status;
    bool available = spektrafilm::probeVulkanRenderer(status);
    std::snprintf(message, size, "%s", status.c_str());
    return available;
  } catch (const std::exception &failure) {
    std::snprintf(message, size, "%s", failure.what());
  } catch (...) {
    std::snprintf(message, size, "Unexpected error initializing the Vulkan renderer");
  }
  return 0;
}
#endif

struct PhotoFilmEffect {
  const char *name;
  float value;
};

struct PhotoFilmParameter {
  const char *name;
  int32_t kind;
  double value[3];
};

struct PhotoFilmStockCalibration {
  float dirSameLayer[3];
  float dirRed[2];
  float dirGreen[2];
  float dirBlue[2];
  float halationStrength[3];
};

// Return exactly the stock data compiled into this renderer. Keep the C++
// ProfileCurveSet layout private so it can change independently of Blender.
extern "C" const char *photo_film_stock_calibration(int32_t index, PhotoFilmStockCalibration *out) {
  const auto *film = spektrafilm::filmProfileCurves(index);
  if (!film || !out) return nullptr;
  std::copy_n(film->dirGammaSameLayerRgb, 3, out->dirSameLayer);
  std::copy_n(film->dirGammaRToGb, 2, out->dirRed);
  std::copy_n(film->dirGammaGToRb, 2, out->dirGreen);
  std::copy_n(film->dirGammaBToRg, 2, out->dirBlue);
  std::copy_n(film->halationStrength, 3, out->halationStrength);
  return film->type;
}

static int render_frame(float *source, float *result, int width, int height,
                        SpektraAppRenderParams &recipe, double frame, char *error, size_t error_size) {
#ifdef _WIN32
  if (!source || !result || width <= 0 || height <= 0 || width > INT32_MAX / 16 ||
      uint64_t(width) * height > UINT32_MAX / 16) {
    std::snprintf(error, error_size, "Invalid film buffers or image dimensions exceed the native buffer range");
    return 0;
  }
#endif
#ifdef __APPLE__
  @autoreleasepool
#endif
  {
    try {
      std::unique_ptr<SpektraRendererHandle, decltype(&SpektraRendererDestroy)> renderer(
          SpektraRendererCreate(), SpektraRendererDestroy);
      if (SpektraRendererIsAvailable(renderer.get())) {
        // The compositor supplies linear pixels. The upstream Developer owns metering.
        recipe.inputColorSpace = SpektraAppLinearRec709ColorSpace();
        recipe.outputColorSpace = SpektraAppLinearRec709ColorSpace();
        recipe.autoExposure = 0;
        SpektraImageBuffer in{source, width, height, width * 16, 4, 4};
        SpektraImageBuffer out{result, width, height, width * 16, 4, 4};
        if (SpektraRendererRender(renderer.get(), &in, &out, &recipe, frame)) return 1;
      }
      std::snprintf(error, error_size, "%s", SpektraRendererLastError(renderer.get()));
    } catch (const std::exception &failure) {
      std::snprintf(error, error_size, "%s", failure.what());
    } catch (...) {
#ifdef _WIN32
      std::snprintf(error, error_size, "Unexpected error in the Vulkan film renderer");
#else
      std::snprintf(error, error_size, "Unexpected error in the Metal film renderer");
#endif
    }
  }
  return 0;
}

extern "C" int photo_film_render_parameters(float *source, float *result, int width, int height,
                                            const PhotoFilmParameter *parameters, size_t count,
                                            double frame, char *error, size_t error_size) {
  auto recipe = SpektraAppMakeDefaultRenderParams();
  for (size_t i = 0; i < count; ++i) {
    const auto &p = parameters[i];
    const auto *v = p.value;
    int applied = 0;
    switch (p.kind) {
      case SpektraAppParamKindInt:
      case SpektraAppParamKindChoice:
      case SpektraAppParamKindFilmStock:
      case SpektraAppParamKindPrintPaper:
        applied = SpektraAppSetIntParam(&recipe, p.name, static_cast<int32_t>(v[0]));
        break;
      case SpektraAppParamKindBool:
        applied = SpektraAppSetBoolParam(&recipe, p.name, v[0] != 0.0);
        break;
      case SpektraAppParamKindDouble:
        applied = SpektraAppSetDoubleParam(&recipe, p.name, v[0]);
        break;
      case SpektraAppParamKindDouble2:
      case SpektraAppParamKindDouble3:
        applied = SpektraAppSetDouble3Param(&recipe, p.name, v[0], v[1], v[2]);
        break;
    }
    if (!applied) {
      std::snprintf(error, error_size, "Unsupported native film parameter: %s", p.name);
      return 0;
    }
  }
  return render_frame(source, result, width, height, recipe, frame, error, error_size);
}

// Preserve the scalar C entry points used by earlier versions of the node.
extern "C" int photo_film_render_effects(float *source, float *result, int width, int height,
                                         int film, int paper, float exposure, float print_exposure,
                                         int grain, int halation, double frame, const PhotoFilmEffect *effects,
                                         size_t effect_count, char *error, size_t error_size) {
  auto recipe = SpektraAppMakeDefaultRenderParams();
  if (film >= 0) recipe.film = film;
  if (paper >= 0) recipe.paper = paper;
  recipe.filmExposureEv = exposure;
  recipe.printExposureEv = print_exposure;
  recipe.grainEnabled = grain;
  recipe.halationEnabled = halation;
  for (size_t i = 0; i < effect_count; ++i) {
    const auto &effect = effects[i];
    if (!SpektraAppSetDoubleParam(&recipe, effect.name, effect.value) &&
        !SpektraAppSetIntParam(&recipe, effect.name, static_cast<int32_t>(effect.value))) {
      std::snprintf(error, error_size, "Unknown native effect parameter: %s", effect.name);
      return 0;
    }
  }
  return render_frame(source, result, width, height, recipe, frame, error, error_size);
}

extern "C" int photo_film_render(float *source, float *result, int width, int height,
                                 int film, int paper, float exposure, float print_exposure,
                                 int grain, int halation, double frame, char *error, size_t error_size) {
  return photo_film_render_effects(source, result, width, height, film, paper, exposure, print_exposure,
                                   grain, halation, frame, nullptr, 0, error, error_size);
}
