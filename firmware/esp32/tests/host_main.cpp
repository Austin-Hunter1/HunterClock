// PC harness for motor_core.h: replays a script from stdin, prints what the
// firmware would have done.  Driven by test_port_matches_python.py, which runs
// the same script through firmware/pico/core.py and compares.
//
//   L <now_us> <json line>     handleLine
//   T <now_us>                 tick
//
// Output, one per line:   P <ch> <dir>    a STEP pulse
//                         J <json>        a message sent to the Pi

#include <stdio.h>
#include <string.h>

#include <iostream>
#include <string>

#include "../src/motor_core.h"

struct HostBoard : public mc::Board {
  void pulse(int ch, int dir) override { printf("P %d %d\n", ch, dir); }
  void setEnable(bool) override {}
  void send(const char* line) override { printf("J %s\n", line); }
  bool loadPositions(int32_t*, int) override { return false; }
  void savePositions(const int32_t*, int) override {}
  const char* bootReason() override { return "power-on"; }
};

int main() {
  HostBoard board;
  mc::MotorCore core(&board, 6);
  core.start(0);
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.size() > 2 && line[0] == 'L') {
      size_t sp = line.find(' ', 2);
      long long now = atoll(line.c_str() + 2);
      std::string json = line.substr(sp + 1);
      core.handleLine(json.c_str(), json.size(), now);
    } else if (line.size() > 2 && line[0] == 'T') {
      core.tick(atoll(line.c_str() + 2));
    }
  }
  return 0;
}
