// The message, memory and multithread suites.
//
// These three are what a plugin uses to talk to the user, to allocate scratch
// memory, and to spread a render over the pool. They share no state with the
// parameter and clip suites, so they live on their own.

#include "ofx/OfxSuitesPriv.h"

#include "core/RenderContext.h"
#include "ofx/OfxProps.h"

#include "ofxMessage.h"
#include "ofxMultiThread.h"

#include <algorithm>
#include <atomic>
#include <climits>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

// Message, memory, multithread suites

static OfxStatus message(void *, const char *type, const char *, const char *fmt, ...) {
  char buf[4096];
  va_list ap;
  va_start(ap, fmt);
  const char *format = "";
  if (fmt) format = fmt;
  vsnprintf(buf, sizeof buf, format, ap);
  va_end(ap);
  if (!type) type = "";
  fprintf(stderr, "OFX %s: %s\n", type, buf);
  // The status bar shows problems and one-shot reports. Log lines stay on
  // stderr, because a render can raise many of them.
  if (!strcmp(type, kOfxMessageError) || !strcmp(type, kOfxMessageFatal) || !strcmp(type, kOfxMessageWarning) ||
      !strcmp(type, kOfxMessageMessage)) {
    if (buf[0]) renderContext().postMessage(buf);
    else renderContext().postMessage("(plugin message)");
  }
  if (!strcmp(type, kOfxMessageQuestion)) return kOfxStatReplyYes;
  return kOfxStatOK;
}
static OfxStatus memoryAlloc(void *, size_t n, void **out) {
  *out = malloc(n);
  if (*out) return kOfxStatOK;
  return kOfxStatErrMemory;
}
static OfxStatus memoryFree(void *p) {
  free(p);
  return kOfxStatOK;
}

static thread_local unsigned tIndex = 0;
static thread_local bool tSpawned = false;
static unsigned cpuCount() { return std::max(1u, std::thread::hardware_concurrency()); }

static OfxStatus multiThreadEphemeral(OfxThreadFunctionV1 f, unsigned n, void *arg) {
  std::vector<std::thread> threads;
  for (unsigned i = 0; i < n; ++i)
    threads.emplace_back([=] {
      tIndex = i;
      tSpawned = true;
      f(i, n, arg);
    });
  for (auto &t : threads) t.join();
  return kOfxStatOK;
}

static OfxStatus multiThread(OfxThreadFunctionV1 f, unsigned n, void *arg) {
  if (!f) return kOfxStatFailed;
  if (n <= 1) {
    tIndex = 0;
    tSpawned = false;
    f(0, 1, arg);
    return kOfxStatOK;
  }
  // A short-lived worker owns one frame; join all suite threads before returning.
  return multiThreadEphemeral(f, n, arg);
}
static OfxStatus multiThreadNumCPUs(unsigned *n) {
  *n = cpuCount();
  return kOfxStatOK;
}
static OfxStatus multiThreadIndex(unsigned *i) {
  *i = tIndex;
  return kOfxStatOK;
}
static int multiThreadIsSpawnedThread() { return tSpawned; }
static std::recursive_mutex *MX(OfxMutexHandle m) { return reinterpret_cast<std::recursive_mutex *>(m); }
static OfxStatus mutexCreate(OfxMutexHandle *m, int lockCount) {
  auto *mx = new std::recursive_mutex;
  for (int i = 0; i < lockCount; ++i) mx->lock();
  *m = reinterpret_cast<OfxMutexHandle>(mx);
  return kOfxStatOK;
}
static OfxStatus mutexDestroy(const OfxMutexHandle m) {
  delete MX(m);
  return kOfxStatOK;
}
static OfxStatus mutexLock(const OfxMutexHandle m) {
  MX(m)->lock();
  return kOfxStatOK;
}
static OfxStatus mutexUnLock(const OfxMutexHandle m) {
  MX(m)->unlock();
  return kOfxStatOK;
}
static OfxStatus mutexTryLock(const OfxMutexHandle m) {
  if (MX(m)->try_lock()) return kOfxStatOK;
  return kOfxStatFailed;
}


static const OfxMessageSuiteV1 gMessageSuite = [] {
  OfxMessageSuiteV1 s{};
  s.message = message;
  return s;
}();
static const OfxMemorySuiteV1 gMemorySuite = [] {
  OfxMemorySuiteV1 s{};
  s.memoryAlloc = memoryAlloc;
  s.memoryFree = memoryFree;
  return s;
}();
static const OfxMultiThreadSuiteV1 gThreadSuite = [] {
  OfxMultiThreadSuiteV1 s{};
  s.multiThread = multiThread;
  s.multiThreadNumCPUs = multiThreadNumCPUs;
  s.multiThreadIndex = multiThreadIndex;
  s.multiThreadIsSpawnedThread = multiThreadIsSpawnedThread;
  s.mutexCreate = mutexCreate;
  s.mutexDestroy = mutexDestroy;
  s.mutexLock = mutexLock;
  s.mutexUnLock = mutexUnLock;
  s.mutexTryLock = mutexTryLock;
  return s;
}();

const OfxMessageSuiteV1 &messageSuiteTable() { return gMessageSuite; }
const OfxMemorySuiteV1 &memorySuiteTable() { return gMemorySuite; }
const OfxMultiThreadSuiteV1 &threadSuiteTable() { return gThreadSuite; }
