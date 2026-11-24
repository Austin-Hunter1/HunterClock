// The GC9A01 round display, wired as in docs/circuit-diagram.html Diagram 4.
//
//   display  ESP32   note
//   VCC      3V3
//   BLK      3V3     backlight, tied on
//   GND      GND
//   SCL      GPIO18  VSPI clock
//   SDA      GPIO23  VSPI MOSI
//   DC       GPIO2   strapping pin: must be low at boot
//   CS       GPIO5   strapping pin: must be high at boot
//   RST      GPIO4
//
// GPIO2 and GPIO5 are boot strapping pins. The display's inputs are high
// impedance so the board boots normally as wired, but if an upload ever fails,
// unplug the DC wire (GPIO2) while flashing.
//
// LovyanGFX is configured here in code rather than through TFT_eSPI's
// User_Setup.h, so the pin map lives in the repo next to the comment that
// explains it instead of inside a library directory that a clean checkout
// would lose.

#pragma once

#define LGFX_USE_V1
#include <LovyanGFX.hpp>

static const int PIN_SCLK = 18;
static const int PIN_MOSI = 23;
static const int PIN_DC   = 2;
static const int PIN_CS   = 5;
static const int PIN_RST  = 4;

static const int DISPLAY_WIDTH = 240;
static const int DISPLAY_HEIGHT = 240;

class ClockDisplay : public lgfx::LGFX_Device {
 public:
  ClockDisplay() {
    {
      auto cfg = bus_.config();
      cfg.spi_host = VSPI_HOST;
      cfg.spi_mode = 0;
      // 40 MHz is what these GC9A01 modules run reliably with dupont wires.
      // 80 MHz works on a short, well-made harness and fails intermittently on
      // a long one, which is the worst kind of failure to chase in a cabinet.
      cfg.freq_write = 40000000;
      cfg.freq_read = 16000000;
      cfg.spi_3wire = false;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = PIN_SCLK;
      cfg.pin_mosi = PIN_MOSI;
      cfg.pin_miso = -1;   // write-only
      cfg.pin_dc = PIN_DC;
      bus_.config(cfg);
      panel_.setBus(&bus_);
    }
    {
      auto cfg = panel_.config();
      cfg.pin_cs = PIN_CS;
      cfg.pin_rst = PIN_RST;
      cfg.pin_busy = -1;
      cfg.panel_width = DISPLAY_WIDTH;
      cfg.panel_height = DISPLAY_HEIGHT;
      cfg.offset_x = 0;
      cfg.offset_y = 0;
      cfg.offset_rotation = 0;
      cfg.readable = false;
      cfg.invert = true;       // GC9A01 panels are inverted
      cfg.rgb_order = false;
      cfg.dlen_16bit = false;
      cfg.bus_shared = false;
      panel_.config(cfg);
    }
    setPanel(&panel_);
  }

 private:
  lgfx::Panel_GC9A01 panel_;
  lgfx::Bus_SPI bus_;
};
