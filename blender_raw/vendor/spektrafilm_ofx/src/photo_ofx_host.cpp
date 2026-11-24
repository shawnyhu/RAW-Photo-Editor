// SPDX-License-Identifier: GPL-3.0-or-later
// Process-isolated still-image OFX host. Plugin bundles remain external and intact.
#include "ofx/OfxHost.h"
#include "ofx/OfxHostPriv.h"
#include "core/Json.h"
#include "core/RenderContext.h"
#include "ofxParam.h"
#include "ofxImageEffect.h"
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <chrono>
#include <atomic>
#include <vector>
#ifdef _WIN32
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace fs = std::filesystem;
void loadBundle(const fs::path &bundle);
// Discovery is intentionally unused: only the selected bundle may be loaded.
fs::path exeDir() { return {}; }

class ParentWatchdog {
#ifdef _WIN32
  HANDLE parent_ = nullptr, stop_ = nullptr, thread_ = nullptr;
  static DWORD WINAPI waitThread(void *opaque) {
    auto *watcher = static_cast<ParentWatchdog *>(opaque);
    const HANDLE handles[] = {watcher->parent_, watcher->stop_};
    if (WaitForMultipleObjects(2, handles, FALSE, INFINITE) == WAIT_OBJECT_0)
      TerminateProcess(GetCurrentProcess(), 125);
    return 0;
  }
#else
  std::thread thread_;
  std::atomic<bool> stop_{false};
#endif
 public:
 ParentWatchdog() {
  const char *parentText = std::getenv("BPE_OFX_PARENT_PID");
  if (!parentText || !*parentText) return;  // standalone CLI use
  char *end = nullptr;
  const auto parent = std::strtoull(parentText, &end, 10);
  if (!end || *end || parent < 1 || parent > 0xffffffffu)
    throw std::runtime_error("Invalid OFX parent process ID");
#ifdef _WIN32
  parent_ = OpenProcess(SYNCHRONIZE, FALSE, static_cast<DWORD>(parent));
  if (!parent_) throw std::runtime_error("OFX worker parent is no longer available");
  stop_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!stop_) { CloseHandle(parent_); parent_ = nullptr; throw std::runtime_error("Cannot create parent watchdog"); }
  // A process handle pins identity even if Windows reuses the numeric PID.
  thread_ = CreateThread(nullptr, 0, waitThread, this, 0, nullptr);
  if (!thread_) {
    CloseHandle(stop_); CloseHandle(parent_); stop_ = parent_ = nullptr;
    throw std::runtime_error("Cannot start parent watchdog");
  }
#else
  if (getppid() != static_cast<pid_t>(parent))
    throw std::runtime_error("OFX worker parent is no longer available");
  thread_ = std::thread([this, parent] {
    while (!stop_.load()) {
      if (getppid() != static_cast<pid_t>(parent)) std::_Exit(125);
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
  });
#endif
 }
 ~ParentWatchdog() {
#ifdef _WIN32
   if (stop_) SetEvent(stop_);
   if (thread_) { WaitForSingleObject(thread_, INFINITE); CloseHandle(thread_); }
#else
   stop_.store(true);
   if (thread_.joinable()) thread_.join();
#endif
#ifdef _WIN32
   if (stop_) CloseHandle(stop_);
   if (parent_) CloseHandle(parent_);
#endif
 }
};

static JsonValue readJson(const fs::path &path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream) throw std::runtime_error("Cannot open JSON request");
  std::string text((std::istreambuf_iterator<char>(stream)), {});
  if (text.size() > 16 * 1024 * 1024) throw std::runtime_error("Request exceeds 16 MB");
  JsonValue result;
  if (!JsonValue::parse(text, result) || result.kind != JsonValue::Kind::Object)
    throw std::runtime_error("Invalid JSON request");
  return result;
}

static double number(const JsonValue &v) {
  double out;
  if (v.kind == JsonValue::Kind::Double) out = v.d;
  else if (v.kind == JsonValue::Kind::Int) out = static_cast<double>(v.i);
  else if (v.kind == JsonValue::Kind::Bool) out = v.b ? 1.0 : 0.0;
  else throw std::runtime_error("Expected numeric parameter value");
  if (!std::isfinite(out)) throw std::runtime_error("Non-finite parameter value");
  return out;
}

static JsonValue valueOf(const Param &p) {
  if (paramIsString(p.kind)) return JsonValue::makeString(p.s);
  auto scalar = [&](double value) {
    if (p.kind == ParamType::Boolean) return JsonValue::makeBool(value != 0);
    if (paramIsInteger(p.kind)) return JsonValue::makeInt(static_cast<long long>(value));
    return JsonValue::makeDouble(value);
  };
  if (p.v.size() == 1) return scalar(p.v[0]);
  JsonValue values = JsonValue::makeArray();
  for (double v : p.v) values.arr.push_back(scalar(v));
  return values;
}

static void assign(Param &p, const JsonValue &v) {
  if (paramIsString(p.kind)) {
    if (v.kind != JsonValue::Kind::String) throw std::runtime_error("Expected string for " + p.name);
    p.s = v.s;
    return;
  }
  if (p.v.empty()) throw std::runtime_error("Parameter is not writable: " + p.name);
  std::vector<double> values;
  if (v.kind == JsonValue::Kind::Array) {
    for (const auto &item : v.arr) values.push_back(number(item));
  } else values.push_back(number(v));
  if (values.size() != p.v.size()) throw std::runtime_error("Wrong parameter dimension: " + p.name);
  for (size_t i = 0; i < values.size(); ++i) {
    const double lo = dprop(p.props, kOfxParamPropMin, static_cast<int>(i), -std::numeric_limits<double>::max());
    double hi = dprop(p.props, kOfxParamPropMax, static_cast<int>(i), std::numeric_limits<double>::max());
    if (p.kind == ParamType::Choice) hi = static_cast<double>(p.ui.choiceLabels.size()) - 1;
    if (values[i] < lo || values[i] > hi || (paramIsInteger(p.kind) && std::floor(values[i]) != values[i]))
      throw std::runtime_error("Parameter outside native range: " + p.name);
  }
  p.v = std::move(values);
}

static void changed(PluginEntry &plugin, Effect *effect, const std::string &name, double frame) {
  PropSet in;
  propSetString(H(&in), kOfxPropType, 0, kOfxTypeParameter);
  propSetString(H(&in), kOfxPropName, 0, name.c_str());
  propSetString(H(&in), kOfxPropChangeReason, 0, kOfxChangeUserEdited);
  propSetDouble(H(&in), kOfxPropTime, 0, frame);
  const double scale[] = {1, 1};
  propSetN<double, propSetDouble>(H(&in), kOfxImageEffectPropRenderScale, 2, scale);
  const auto status = callAction(plugin.plugin, kOfxActionInstanceChanged, effect, &in);
  if (!ofxActionOk(status)) throw std::runtime_error("Plugin rejected parameter change: " + name);
}

static void apply(PluginEntry &plugin, Effect *effect, const JsonValue &request) {
  const auto *params = request.find("parameters");
  if (!params) return;
  if (params->kind != JsonValue::Kind::Object) throw std::runtime_error("parameters must be an object");
  // Apply user edits in order so native dependent controls and stock calibration run.
  for (const auto &item : params->obj) {
    Param *p = findParam(effect, item.first.c_str());
    if (!p) throw std::runtime_error("Unknown OFX parameter: " + item.first);
    assign(*p, item.second);
    changed(plugin, effect, item.first, request.number("frame", 0));
    refreshParamUiCache(effect);
  }
}

static JsonValue describe(PluginEntry &plugin, Effect *effect) {
  refreshParamUiCache(effect);
  JsonValue root = JsonValue::makeObject();
  root.set("identifier", JsonValue::makeString(plugin.plugin->pluginIdentifier));
  root.set("plugin", JsonValue::makeString(plugin.plugin->pluginIdentifier));
  root.set("label", JsonValue::makeString(plugin.label));
  root.set("version_major", JsonValue::makeInt(plugin.plugin->pluginVersionMajor));
  root.set("version_minor", JsonValue::makeInt(plugin.plugin->pluginVersionMinor));
  root.set("host_schema", JsonValue::makeInt(1));
  const auto *version = findParam(effect, "infoVersion");
  root.set("version", JsonValue::makeString(version ? version->s : sprop(effect->props, kOfxPropVersionLabel)));
  JsonValue parameters = JsonValue::makeArray();
  for (const auto &ptr : effect->params) {
    const auto &p = *ptr;
    JsonValue item = JsonValue::makeObject();
    item.set("name", JsonValue::makeString(p.name));
    item.set("type", JsonValue::makeString(p.type));
    item.set("label", JsonValue::makeString(p.ui.label));
    item.set("hint", JsonValue::makeString(p.ui.hint));
    item.set("group", JsonValue::makeString(p.ui.parent));
    item.set("secret", JsonValue::makeBool(p.ui.secret));
    item.set("enabled", JsonValue::makeBool(p.ui.enabled));
    item.set("string_label", JsonValue::makeBool(p.ui.stringIsLabel));
    item.set("default", valueOf(p));
    item.set("value", valueOf(p));
    item.set("current", valueOf(p));
    item.set("dimension", JsonValue::makeInt(paramDimension(p.kind)));
    item.set("minimum", JsonValue::makeDouble(dprop(p.props, kOfxParamPropMin, 0, -1e10)));
    item.set("maximum", JsonValue::makeDouble(dprop(p.props, kOfxParamPropMax, 0, 1e10)));
    item.set("display_minimum", JsonValue::makeDouble(dprop(p.props, kOfxParamPropDisplayMin, 0, p.ui.displayMin)));
    item.set("display_maximum", JsonValue::makeDouble(dprop(p.props, kOfxParamPropDisplayMax, 0, p.ui.displayMax)));
    JsonValue options = JsonValue::makeArray();
    for (const auto &label : p.ui.choiceLabels) options.arr.push_back(JsonValue::makeString(label));
    item.set("options", std::move(options));
    parameters.arr.push_back(std::move(item));
  }
  root.set("parameters", std::move(parameters));
  return root;
}

static void writeJson(const fs::path &path, const JsonValue &value) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  out << value.dump();
  if (!out) throw std::runtime_error("Cannot write descriptor JSON");
}

static int run(const std::vector<std::string> &args) {
  if (args.size() < 4) throw std::runtime_error("Usage: photo_ofx_host describe bundle output.json [request.json] | render bundle request.json input.f32 output.f32");
  const std::string command = args[1];
  const fs::path bundle = fs::u8path(args[2]);
  if (!fs::is_directory(bundle) || bundle.extension() != ".bundle") throw std::runtime_error("Select an intact OFX bundle directory");
  fs::path lutStage;
  if (command == "export-lut") {
    if (args.size() != 5) throw std::runtime_error("Usage: photo_ofx_host export-lut BUNDLE REQUEST.json OUTPUT.cube");
    lutStage = fs::u8path(args[3]).parent_path() / "native-lut-export";
    if (!fs::create_directory(lutStage)) throw std::runtime_error("LUT staging directory already exists");
    // Set before loading the plugin: its C runtime may cache the process environment.
#ifdef _WIN32
    if (_wputenv_s(L"USERPROFILE", lutStage.c_str())) throw std::runtime_error("Cannot isolate native LUT output");
#else
    if (setenv("HOME", lutStage.c_str(), 1)) throw std::runtime_error("Cannot isolate native LUT output");
#endif
  }
  renderContext().setMessageSink([](const std::string &text) { std::cerr << text << '\n'; });
  loadBundle(bundle);
  if (plugins().size() != 1) throw std::runtime_error("Bundle did not expose one supported OFX filter");
  auto &plugin = plugins()[0];
  if (std::string(plugin.plugin->pluginIdentifier).find("spektrafilm") == std::string::npos)
    throw std::runtime_error("This helper supports Spektrafilm OFX bundles only");
  auto effect = createInstance(plugin);
  if (!effect) throw std::runtime_error("OFX plugin instance creation failed");
  struct Cleanup { PluginEntry &plugin; Effect *effect; ~Cleanup() { callAction(plugin.plugin, kOfxActionDestroyInstance, effect); } } cleanup{plugin, effect.get()};
  if (command == "describe") {
    if (args.size() > 4) apply(plugin, effect.get(), readJson(fs::u8path(args[4])));
    writeJson(fs::u8path(args[3]), describe(plugin, effect.get()));
    return 0;
  }
  if (command == "export-lut") {
    if (args.size() != 5) throw std::runtime_error("Usage: photo_ofx_host export-lut BUNDLE REQUEST.json OUTPUT.cube");
    const fs::path requestPath = fs::u8path(args[3]);
    const fs::path target = fs::u8path(args[4]);
    JsonValue request = readJson(requestPath);
    const auto size = request.integer("lut_size", 33);
    if (size != 33 && size != 65) throw std::runtime_error("LUT size must be 33 or 65");
    if (target.extension() != ".cube") throw std::runtime_error("LUT destination must use .cube");
    fs::path licenseTarget = target;
    licenseTarget += ".LICENSE.txt";
    if (!request.boolean("overwrite", false) && (fs::exists(target) || fs::exists(licenseTarget)))
      throw std::runtime_error("LUT output already exists");
    const auto license = bundle / "Contents" / "Resources" / "Legal" / "SPEKTRAFILM_OFX_LUT_LICENSE.txt";
    if (!fs::is_regular_file(license)) throw std::runtime_error("The official bundle's exported LUT license is missing");
    apply(plugin, effect.get(), request);
    const fs::path stage = lutStage;
    // Only this helper's home is redirected.
    // Official callbacks remain responsible for sampling and disabling spatial effects.
    for (const auto &field : std::vector<std::pair<std::string, JsonValue>>{
           {"lutDestination", JsonValue::makeInt(0)},
           {"lutSize", JsonValue::makeInt(size == 65 ? 1 : 0)},
           {"lutIdentifier", JsonValue::makeString("BlenderPhotoEditor")}}) {
      auto *parameter = findParam(effect.get(), field.first.c_str());
      if (!parameter) throw std::runtime_error("Official plugin does not provide native LUT export");
      assign(*parameter, field.second);
    }
    changed(plugin, effect.get(), "exportLut", request.number("frame", 0));
    std::vector<fs::path> cubes;
    for (const auto &entry : fs::recursive_directory_iterator(stage))
      if (entry.is_regular_file() && entry.path().extension() == ".cube") cubes.push_back(entry.path());
    if (cubes.size() != 1) throw std::runtime_error("Native plugin did not create one LUT in the isolated output directory");
    const auto policy = request.boolean("overwrite", false) ? fs::copy_options::overwrite_existing : fs::copy_options::none;
    fs::copy_file(license, licenseTarget, policy);
    fs::copy_file(cubes[0], target, policy);
    const auto metadataPath = request.text("result_metadata", "");
    if (!metadataPath.empty()) writeJson(fs::u8path(metadataPath), describe(plugin, effect.get()));
    return 0;
  }
  if (command != "render" || args.size() != 6) throw std::runtime_error("Invalid host command");
  JsonValue request = readJson(fs::u8path(args[3]));
  const auto w = request.integer("width", 0), h = request.integer("height", 0);
  const double frame = request.number("frame", 0);
  if (w < 1 || h < 1 || w > 65536 || h > 65536 || w * h > 268435455 || !std::isfinite(frame))
    throw std::runtime_error("Invalid frame size or time");
  apply(plugin, effect.get(), request);
  {
    std::lock_guard<std::mutex> lock(effect->dimMutex);
    effect->w = effect->outW = static_cast<int>(w);
    effect->h = effect->outH = static_cast<int>(h);
  }
  const auto metadataPath = request.text("result_metadata", "");
  if (!metadataPath.empty()) writeJson(fs::u8path(metadataPath), describe(plugin, effect.get()));
  const size_t count = static_cast<size_t>(w * h * 4), bytes = count * sizeof(float);
  std::vector<float> source(count), result(count, std::numeric_limits<float>::quiet_NaN());
  std::ifstream input(fs::u8path(args[4]), std::ios::binary | std::ios::ate);
  if (!input || input.tellg() != static_cast<std::streamoff>(bytes)) throw std::runtime_error("Input float buffer has wrong byte size");
  input.seekg(0);
  input.read(reinterpret_cast<char *>(source.data()), static_cast<std::streamsize>(bytes));
  if (!input) throw std::runtime_error("Failed to read frame");
  for (float value : source) if (!std::isfinite(value)) throw std::runtime_error("Input contains non-finite pixels");
  // File exchange is top-down; OFX's native origin is bottom-left.
  const size_t row = static_cast<size_t>(w) * 4;
  for (size_t y = 0; y < static_cast<size_t>(h) / 2; ++y)
    std::swap_ranges(source.begin() + y * row, source.begin() + (y + 1) * row,
                     source.begin() + (static_cast<size_t>(h) - y - 1) * row);
  PropSet sequence;
  const double frames[] = {frame, frame}, scale[] = {1, 1};
  propSetN<double, propSetDouble>(H(&sequence), kOfxImageEffectPropFrameRange, 2, frames);
  propSetDouble(H(&sequence), kOfxImageEffectPropFrameStep, 0, 1);
  propSetN<double, propSetDouble>(H(&sequence), kOfxImageEffectPropRenderScale, 2, scale);
  callAction(plugin.plugin, kOfxImageEffectActionBeginSequenceRender, effect.get(), &sequence);
  const auto status = renderEffect(plugin.plugin, effect.get(), source.data(), result.data(), static_cast<int>(w), static_cast<int>(h), static_cast<int>(w), static_cast<int>(h), 0, nullptr, nullptr, false, 1.0, frame);
  callAction(plugin.plugin, kOfxImageEffectActionEndSequenceRender, effect.get(), &sequence);
  if (status != kOfxStatOK) throw std::runtime_error("OFX render failed with status " + std::to_string(status));
  for (float value : result) if (!std::isfinite(value)) throw std::runtime_error("OFX returned non-finite pixels");
  for (size_t y = 0; y < static_cast<size_t>(h) / 2; ++y)
    std::swap_ranges(result.begin() + y * row, result.begin() + (y + 1) * row,
                     result.begin() + (static_cast<size_t>(h) - y - 1) * row);
  std::ofstream output(fs::u8path(args[5]), std::ios::binary | std::ios::trunc);
  output.write(reinterpret_cast<const char *>(result.data()), static_cast<std::streamsize>(bytes));
  if (!output) throw std::runtime_error("Failed to write output frame");
  return 0;
}

#ifdef _WIN32
int wmain(int argc, wchar_t **argv) {
  std::vector<std::string> args;
  for (int i = 0; i < argc; ++i) {
    const int size = WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, nullptr, 0, nullptr, nullptr);
    std::string text(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, text.data(), size, nullptr, nullptr);
    text.pop_back(); args.push_back(std::move(text));
  }
#else
int main(int argc, char **argv) {
  std::vector<std::string> args(argv, argv + argc);
#endif
  int status = 1;
  try { ParentWatchdog watchdog; status = run(args); }
  catch (const std::exception &error) { std::cerr << "Spektrafilm OFX: " << error.what() << '\n'; }
  std::cout.flush(); std::cerr.flush();
  // All frame files are closed, the instance is destroyed and our watchdog joined.
  // Official 0.5.0 starts an asynchronous online-update task on first use. Its DLL
  // detach may block a completed worker on a network timeout. End only this
  // disposable host process; the OS releases its remaining plugin/module state.
#ifdef _WIN32
  TerminateProcess(GetCurrentProcess(), static_cast<UINT>(status));
#else
  std::_Exit(status);
#endif
  return status;
}
