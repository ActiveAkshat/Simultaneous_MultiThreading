#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "jobs.h"

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: %s <serial>\n", argv[0]);
    return 1;
  }
  uint32_t serial = strtoul(argv[1], NULL, 10);
  jobs_init(serial);

  // ----------------------------------
  // Your experiments go here.
  // ----------------------------------

  return 0;
}
