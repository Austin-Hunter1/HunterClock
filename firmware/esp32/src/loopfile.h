// Reader for the .wcl frame-loop container written by tools/make_loop.py.
//
// The layout is specified in tools/loopfile.py and checked byte for byte by
// controller/tests/test_loopfile.py. If you change one, change all three:
//
//   offset  size  field
//   0       4     magic "WCLP"
//   4       2     version (1)
//   6       2     frame count N
//   8       2     width
//   10      2     height
//   12      2     fps
//   14      2     reserved
//   16      4*N   frame offsets from the start of the file
//   16+4N   4*N   frame lengths
//   ...           JPEG payloads
//
// Everything is little-endian, which is the ESP32's native order too.
//
// The index is held in RAM (8 bytes per frame; a 100-frame loop costs 800
// bytes) and the payloads stay on flash, read one frame at a time into a
// reusable buffer. That is what keeps a 1.5 MB loop playable on a board with
// a few hundred KB of usable heap.

#pragma once

#include <Arduino.h>
#include <FS.h>

static const uint32_t WCL_MAGIC = 0x504C4357;  // "WCLP", little-endian
static const uint16_t WCL_VERSION = 1;
static const uint16_t WCL_MAX_FRAMES = 600;    // ~60 s at 10 fps; a sanity bound

class LoopFile {
 public:
  bool open(fs::FS &fs, const char *path) {
    close();
    file_ = fs.open(path, "r");
    if (!file_) {
      snprintf(error_, sizeof(error_), "cannot open %s", path);
      return false;
    }

    uint8_t header[16];
    if (file_.read(header, sizeof(header)) != (int)sizeof(header)) {
      snprintf(error_, sizeof(error_), "%s is too short", path);
      close();
      return false;
    }

    const uint32_t magic = read32(header + 0);
    const uint16_t version = read16(header + 4);
    count_ = read16(header + 6);
    width_ = read16(header + 8);
    height_ = read16(header + 10);
    fps_ = read16(header + 12);

    if (magic != WCL_MAGIC) {
      snprintf(error_, sizeof(error_), "%s is not a .wcl file", path);
      close();
      return false;
    }
    if (version != WCL_VERSION) {
      snprintf(error_, sizeof(error_), "loop version %u, firmware understands %u",
               (unsigned)version, (unsigned)WCL_VERSION);
      close();
      return false;
    }
    if (count_ == 0 || count_ > WCL_MAX_FRAMES) {
      snprintf(error_, sizeof(error_), "loop claims %u frames", (unsigned)count_);
      close();
      return false;
    }
    if (fps_ == 0) fps_ = 10;

    offsets_ = (uint32_t *)malloc(sizeof(uint32_t) * count_);
    lengths_ = (uint32_t *)malloc(sizeof(uint32_t) * count_);
    if (!offsets_ || !lengths_) {
      snprintf(error_, sizeof(error_), "no memory for a %u-frame index", (unsigned)count_);
      close();
      return false;
    }

    const size_t index_bytes = sizeof(uint32_t) * count_;
    if (file_.read((uint8_t *)offsets_, index_bytes) != (int)index_bytes ||
        file_.read((uint8_t *)lengths_, index_bytes) != (int)index_bytes) {
      snprintf(error_, sizeof(error_), "truncated index");
      close();
      return false;
    }

    // The largest frame decides the read buffer, allocated once.
    largest_ = 0;
    const uint32_t size = (uint32_t)file_.size();
    for (uint16_t i = 0; i < count_; i++) {
      if (offsets_[i] + lengths_[i] > size) {
        snprintf(error_, sizeof(error_), "frame %u runs past the end", (unsigned)i);
        close();
        return false;
      }
      if (lengths_[i] > largest_) largest_ = lengths_[i];
    }
    buffer_ = (uint8_t *)malloc(largest_);
    if (!buffer_) {
      snprintf(error_, sizeof(error_), "no memory for a %u-byte frame", (unsigned)largest_);
      close();
      return false;
    }
    error_[0] = '\0';
    return true;
  }

  // Read one frame into the internal buffer. Returns nullptr on a read error.
  const uint8_t *frame(uint16_t index, uint32_t *length_out) {
    if (!file_ || index >= count_ || !buffer_) return nullptr;
    if (!file_.seek(offsets_[index])) return nullptr;
    const uint32_t length = lengths_[index];
    if (file_.read(buffer_, length) != (int)length) return nullptr;
    if (length_out) *length_out = length;
    return buffer_;
  }

  void close() {
    if (file_) file_.close();
    free(offsets_);  offsets_ = nullptr;
    free(lengths_);  lengths_ = nullptr;
    free(buffer_);   buffer_ = nullptr;
    count_ = width_ = height_ = fps_ = 0;
    largest_ = 0;
  }

  bool ok() const { return count_ > 0 && buffer_ != nullptr; }
  uint16_t count() const { return count_; }
  uint16_t width() const { return width_; }
  uint16_t height() const { return height_; }
  uint16_t fps() const { return fps_ ? fps_ : 10; }
  uint32_t frameIntervalMs() const { return 1000UL / fps(); }
  const char *error() const { return error_; }

 private:
  static uint16_t read16(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
  }
  static uint32_t read32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
  }

  fs::File file_;
  uint32_t *offsets_ = nullptr;
  uint32_t *lengths_ = nullptr;
  uint8_t *buffer_ = nullptr;
  uint32_t largest_ = 0;
  uint16_t count_ = 0, width_ = 0, height_ = 0, fps_ = 0;
  char error_[64] = {0};
};
