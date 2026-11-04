// cpp_check.cpp: podcrp.h from C++, the way the .ino files include it. build.sh links this against
// podcrp.c and the engine compiled as C, so a missing extern "C" fails the link, not the pod.
#include "../podcrp.h"
#include <cstdio>
#include <cstring>

static podcrp_link_t podLink;
static uint8_t sent[64];
static uint32_t sentLen = 0;

static uint32_t sendHook(podcrp_link_t *, const uint8_t *p, uint32_t len) {
  if (len > sizeof sent) return 0;
  memcpy(sent, p, len);
  sentLen = len;
  return len;
}

int main() {
  podcrp_hooks_t hooks;
  memset(&hooks, 0, sizeof hooks);
  hooks.send = sendHook;
  podcrp_init(&podLink, PODCRP_BLE, &hooks, PODCRP_DEVICE_ID, "BarPod-TEST");
  const uint8_t hello[10] = {0x01, 0x0B, 0x04, 0x00, 0x02, 0, 0, 0, 0, 0};
  podcrp_rx_packet(&podLink, hello, sizeof hello);
  podcrp_pass(&podLink, 1000);
  const uint8_t want[10] = {0x01, 0x0B, 0x01, 0xF4, 0x00, 0x01, 0x42, 0x50, 0x4F, 0x44};
  bool ok = sentLen == 10 && memcmp(sent, want, 10) == 0 && podcrp_in_session(&podLink);
  printf("%s  C++: podcrp.h compiles as C++ (gnu++11, as the .ino files), links to the C objects and answers HELLO\n",
         ok ? "PASS" : "FAIL");
  return ok ? 0 : 1;
}
